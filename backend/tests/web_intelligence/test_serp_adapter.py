"""The Bright Data SERP adapter, with no network anywhere.

The real httpx client is driven through a MockTransport, so what these
tests exercise is the actual request shape and the actual response
handling -- only the wire is fake. A test that reached Bright Data would
fail on the unrouted-request assertion rather than silently spend money.

THE PROPERTY THIS FILE EXISTS FOR: a successful empty search and a
provider failure are different outcomes, and neither can be mistaken for
the other.
"""

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.integrations.brightdata.serp import (
    BrightDataSerpWebSearchProvider,
    build_google_search_url,
)
from app.web_intelligence.acquisition import (
    WebSearchError,
    WebSearchInvalidResponseError,
    WebSearchProviderUnavailableError,
    WebSearchTimeoutError,
)
from app.web_intelligence.normalization import WebRecordNormalizationError
from app.web_intelligence.schemas import (
    DEFAULT_LOCALE,
    INDIA_LOCALE,
    MAX_RESULTS_PER_QUERY,
)

QUERY = "restaurant demand forecasting software"

ORGANIC = {
    "organic": [
        {
            "title": "AI-Forecasting Software",
            "link": "https://www.crunchtime.com/restaurant-forecasting",
            "description": "Forecast demand and cut waste.",
            "global_rank": 1,
        },
        {
            "title": "Inventory Forecasting for Restaurants",
            "link": "https://marketman.com/forecasting?utm_source=serp",
            "description": "Predict par levels automatically.",
            "global_rank": 2,
        },
    ]
}


class Wire:
    """A scripted Bright Data wire, recording every request body served."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status: int = 200,
        text: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.payload = ORGANIC if payload is None else payload
        self.status = status
        self.text = text
        self.raises = raises
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.raises is not None:
            raise self.raises
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.payload)


def provider_for(wire: Wire) -> BrightDataSerpWebSearchProvider:
    return BrightDataSerpWebSearchProvider(
        settings=Settings(
            _env_file=None,
            BRIGHTDATA_API_KEY="test-token-do-not-log",
            BRIGHTDATA_BASE_URL="https://api.brightdata.test",
            BRIGHTDATA_SERP_ZONE="serp_zone_test",
        ),
        transport=httpx.MockTransport(wire),
    )


# -- the request shape ------------------------------------------------------


def test_one_query_is_one_request() -> None:
    """No pagination is possible by accident."""
    wire = Wire()

    provider_for(wire).search_web(QUERY)

    assert len(wire.requests) == 1


def test_the_request_targets_the_serp_api() -> None:
    wire = Wire()

    provider_for(wire).search_web(QUERY)

    request = wire.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.brightdata.test/request"


def test_the_request_asks_for_parsed_organic_results_only() -> None:
    """`parsed_light` excludes ads and knowledge panels at the provider."""
    import json

    wire = Wire()

    provider_for(wire).search_web(QUERY)

    body = json.loads(wire.requests[0].content)
    assert body["zone"] == "serp_zone_test"
    assert body["format"] == "raw"
    assert body["data_format"] == "parsed_light"


def test_the_search_url_carries_the_query_and_locale() -> None:
    import json
    from urllib.parse import parse_qs, urlsplit

    wire = Wire()

    provider_for(wire).search_web(QUERY, limit=5, locale=INDIA_LOCALE)

    body = json.loads(wire.requests[0].content)
    params = parse_qs(urlsplit(body["url"]).query)
    assert params["q"] == [QUERY]
    assert params["gl"] == ["in"]
    assert params["hl"] == ["en"]
    assert params["num"] == ["5"]
    assert body["country"] == "in"


def test_the_search_url_never_paginates() -> None:
    """Page 0 only, as a property of the code rather than a convention."""
    url = build_google_search_url(QUERY, limit=10, locale=DEFAULT_LOCALE)

    assert "start=" not in url
    assert "page=" not in url


def test_the_default_locale_is_used_when_none_is_given() -> None:
    import json
    from urllib.parse import parse_qs, urlsplit

    wire = Wire()

    provider_for(wire).search_web(QUERY)

    body = json.loads(wire.requests[0].content)
    params = parse_qs(urlsplit(body["url"]).query)
    assert params["gl"] == [DEFAULT_LOCALE.country]


def test_the_credential_is_sent_as_a_bearer_header() -> None:
    wire = Wire()

    provider_for(wire).search_web(QUERY)

    assert wire.requests[0].headers["authorization"].startswith("Bearer ")


def test_no_discovered_url_is_ever_fetched() -> None:
    """DISCOVERY ONLY. The adapter's whole network footprint is one POST.

    If it opened results, this wire would see three requests -- one
    search and two pages -- rather than one.
    """
    wire = Wire()

    records = provider_for(wire).search_web(QUERY)

    assert len(records) == 2
    assert len(wire.requests) == 1
    assert all("brightdata.test" in str(r.url) for r in wire.requests)


# -- successful results -----------------------------------------------------


def test_organic_results_become_normalized_records() -> None:
    records = provider_for(Wire()).search_web(QUERY)

    assert [record.domain for record in records] == [
        "crunchtime.com",
        "marketman.com",
    ]
    # Normalization applied: the tracking parameter is gone.
    assert records[1].url == "https://marketman.com/forecasting"
    assert all(record.query == QUERY for record in records)


def test_a_successful_search_with_no_results_is_an_empty_list() -> None:
    """THE DISTINCTION. The engine answered, and there is nothing."""
    records = provider_for(Wire({"organic": []})).search_web(QUERY)

    assert records == []


def test_a_response_with_no_organic_key_is_also_an_empty_list() -> None:
    assert provider_for(Wire({})).search_web(QUERY) == []


def test_the_limit_is_passed_through_to_normalization() -> None:
    records = provider_for(Wire()).search_web(QUERY, limit=1)
    assert len(records) == 1


# -- failure is never an empty list -----------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_error_raises(status: int) -> None:
    with pytest.raises(WebSearchProviderUnavailableError):
        provider_for(Wire(status=status)).search_web(QUERY)


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_failure_raises_without_echoing_the_body(status: int) -> None:
    """An auth failure's body is the likeliest place for a token to appear."""
    with pytest.raises(WebSearchProviderUnavailableError) as caught:
        provider_for(Wire(status=status, payload={"error": "leaky"})).search_web(
            QUERY
        )

    assert "leaky" not in str(caught.value)
    assert "test-token-do-not-log" not in str(caught.value)


def test_a_client_error_raises() -> None:
    with pytest.raises(WebSearchError):
        provider_for(Wire(status=422)).search_web(QUERY)


def test_a_timeout_raises_a_timeout() -> None:
    """Kept apart from a plain failure: the request may have been billed."""
    wire = Wire(raises=httpx.ReadTimeout("too slow"))

    with pytest.raises(WebSearchTimeoutError):
        provider_for(wire).search_web(QUERY)


def test_a_connection_failure_raises_unavailable() -> None:
    wire = Wire(raises=httpx.ConnectError("no route"))

    with pytest.raises(WebSearchProviderUnavailableError):
        provider_for(wire).search_web(QUERY)


def test_an_unreadable_body_raises_rather_than_returning_nothing() -> None:
    """Not the same as a response containing no organic results."""
    with pytest.raises(WebSearchInvalidResponseError):
        provider_for(Wire(text="<html>not json</html>")).search_web(QUERY)


def test_every_failure_is_a_web_search_error() -> None:
    """Orchestration catches one type and nothing vendor-specific."""
    for wire in (
        Wire(status=500),
        Wire(status=401),
        Wire(raises=httpx.ReadTimeout("slow")),
        Wire(text="nonsense"),
    ):
        with pytest.raises(WebSearchError):
            provider_for(wire).search_web(QUERY)


# -- input validation happens before the wire -------------------------------


def test_a_blank_query_never_reaches_the_provider() -> None:
    wire = Wire()

    with pytest.raises(WebRecordNormalizationError):
        provider_for(wire).search_web("   ")

    assert wire.requests == []


def test_an_out_of_range_limit_never_reaches_the_provider() -> None:
    wire = Wire()

    with pytest.raises(WebRecordNormalizationError):
        provider_for(wire).search_web(QUERY, limit=MAX_RESULTS_PER_QUERY + 1)

    assert wire.requests == []
