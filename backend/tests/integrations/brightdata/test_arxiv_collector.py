"""The arXiv research adapter, with no network anywhere.

The Bright Data transport is driven through httpx.MockTransport, so what
these tests exercise is the real client against a fake wire -- not a
stubbed client. A test that reached the provider would fail on the
unrouted-request assertion rather than silently cost a job.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.collection.schemas import PollingPolicy
from app.config import Settings
from app.integrations.brightdata.arxiv import (
    ARXIV_COLLECTOR_ID,
    ARXIV_RESULT_SIZE,
    BrightDataArxivCollector,
    build_arxiv_search_url,
)
from app.integrations.brightdata.client import BrightDataClient
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollectionResult,
    ResearchCollector,
)

JOB_ID = "j_msyy0tyn18aapzwpey"
NOW = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)

PAPER: dict[str, Any] = {
    "arxiv_id": "2608.13083",
    "title": "AoI-Guaranteed Dynamic Route Planning for Connected Vehicles",
    "abstract": "A dual-factor approach to route planning.",
    "authors": ["Sajedeh Norouzi"],
    "published_at": "2026-08-13",
    "categories": ["Systems and Control (eess.SY)"],
    "paper_url": "https://arxiv.org/abs/2608.13083",
    "pdf_url": "https://arxiv.org/pdf/2608.13083",
    "query": "dynamic vehicle routing",
    "input": {"url": "https://arxiv.org/search/?query=dynamic+vehicle+routing"},
}


class Provider:
    """A scripted Bright Data wire, recording every request it served."""

    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        running_polls: int = 0,
        trigger_status: int = 200,
        dataset_status: int = 200,
    ) -> None:
        self.records = [PAPER] if records is None else records
        self.running_polls = running_polls
        self.trigger_status = trigger_status
        self.dataset_status = dataset_status
        self.triggers: list[dict[str, Any]] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dca/trigger":
            self.triggers.append(
                {
                    "collector": request.url.params.get("collector"),
                    "body": json.loads(request.content),
                }
            )
            if self.trigger_status != 200:
                return httpx.Response(self.trigger_status, json={"error": "nope"})
            return httpx.Response(200, json={"collection_id": JOB_ID})

        if request.url.path == "/dca/dataset":
            self.polls += 1
            if self.dataset_status != 200:
                return httpx.Response(self.dataset_status, json={"error": "nope"})
            if self.polls <= self.running_polls:
                return httpx.Response(200, json={"status": "collecting"})
            return httpx.Response(200, json=self.records)

        raise AssertionError(f"unexpected Bright Data call: {request.url}")


def collector_for(
    provider: Provider, *, polling: PollingPolicy | None = None
) -> tuple[BrightDataArxivCollector, list[float]]:
    """An adapter over the scripted wire. Returns it and the sleeps taken."""
    client = BrightDataClient(
        settings=Settings(
            _env_file=None,
            BRIGHTDATA_API_KEY="test-token-do-not-log",
            BRIGHTDATA_BASE_URL="https://api.brightdata.test",
        ),
        transport=httpx.MockTransport(provider),
    )
    slept: list[float] = []
    clock = {"t": NOW}

    def now() -> datetime:
        return clock["t"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += timedelta(seconds=seconds)

    return (
        BrightDataArxivCollector(
            client,
            polling=polling
            or PollingPolicy(interval_seconds=1.0, timeout_seconds=60.0),
            now=now,
            sleep=sleep,
        ),
        slept,
    )


# -- URL construction -------------------------------------------------------


def test_the_validated_url_shape_is_produced() -> None:
    assert build_arxiv_search_url("urban freight optimization") == (
        "https://arxiv.org/search/?query=urban%20freight%20optimization"
        "&searchtype=all&abstracts=show&order=-announced_date_first&size=15"
    )


def test_spaces_are_percent_encoded_not_form_encoded() -> None:
    """The shape the collector was validated against."""
    url = build_arxiv_search_url("vehicle routing demand forecasting")

    assert "%20" in url
    assert "+" not in urlsplit(url).query.split("&")[0]


@pytest.mark.parametrize(
    "query",
    ['C++ & "deep" learning', "café/über: 100% coverage", "a&b=c?d#e", "α-β pruning"],
)
def test_special_characters_survive_encoding(query: str) -> None:
    url = build_arxiv_search_url(query)

    assert parse_qs(urlsplit(url).query)["query"] == [query]


def test_surrounding_and_repeated_whitespace_is_collapsed() -> None:
    assert build_arxiv_search_url("  urban   freight  ") == build_arxiv_search_url(
        "urban freight"
    )


def test_the_search_parameters_are_the_validated_ones() -> None:
    params = parse_qs(urlsplit(build_arxiv_search_url("x")).query)

    assert params["searchtype"] == ["all"]
    assert params["abstracts"] == ["show"]
    assert params["order"] == ["-announced_date_first"]
    assert params["size"] == [str(ARXIV_RESULT_SIZE)]


def test_different_queries_produce_different_urls() -> None:
    """Dynamic queries are the whole point of the frozen collector."""
    urls = {
        build_arxiv_search_url(q)
        for q in (
            "on-demand allocation urban freight",
            "urban freight optimization",
            "vehicle routing demand forecasting",
        )
    }

    assert len(urls) == 3


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_query_is_refused_before_any_provider_call(blank: str) -> None:
    """A blank query builds a valid URL returning arXiv's entire listing."""
    provider = Provider()
    collector, _ = collector_for(provider)

    with pytest.raises(ResearchCollectionError, match="must not be blank"):
        collector.search(blank)

    assert provider.triggers == []


# -- the happy path ---------------------------------------------------------


def test_the_frozen_collector_id_is_used() -> None:
    provider = Provider()
    collector, _ = collector_for(provider)

    collector.search("urban freight optimization")

    assert [t["collector"] for t in provider.triggers] == [ARXIV_COLLECTOR_ID]
    assert ARXIV_COLLECTOR_ID == "c_msz192rbcrk2ki1vj"


def test_the_search_url_is_submitted_as_the_collector_input() -> None:
    provider = Provider()
    collector, _ = collector_for(provider)

    collector.search("urban freight optimization")

    assert provider.triggers[0]["body"] == [
        {"url": build_arxiv_search_url("urban freight optimization")}
    ]


def test_the_caller_query_is_returned_unchanged_as_provenance() -> None:
    """The record-level `query` is correct now, and still not authoritative."""
    provider = Provider()
    collector, _ = collector_for(provider)

    result = collector.search("urban freight optimization")

    assert result.query == "urban freight optimization"
    assert result.records[0]["query"] == "dynamic vehicle routing"


def test_raw_records_pass_through_unnormalized() -> None:
    """Normalization belongs to the ingestion layer, not the adapter."""
    provider = Provider()
    collector, _ = collector_for(provider)

    result = collector.search("urban freight optimization")

    assert result.records == [PAPER]
    assert "input" in result.records[0]


def test_the_provider_job_id_propagates() -> None:
    provider = Provider()
    collector, _ = collector_for(provider)

    assert collector.search("urban freight").provider_job_id == JOB_ID


def test_searched_at_propagates_and_is_timezone_aware() -> None:
    provider = Provider()
    collector, _ = collector_for(provider)

    searched_at = collector.search("urban freight").searched_at

    assert searched_at is not None
    assert searched_at.tzinfo is not None
    assert searched_at >= NOW


def test_an_empty_dataset_is_a_success_not_an_error() -> None:
    """ "No papers for this query" is not "the provider broke"."""
    provider = Provider(records=[])
    collector, _ = collector_for(provider)

    result = collector.search("a genuinely obscure topic")

    assert result.records == []
    assert result.provider_job_id == JOB_ID


# -- waiting ----------------------------------------------------------------


def test_a_job_still_running_is_polled_until_it_completes() -> None:
    provider = Provider(running_polls=3)
    collector, slept = collector_for(provider)

    result = collector.search("urban freight")

    assert result.records == [PAPER]
    assert slept == [1.0, 1.0, 1.0]


def test_exhausting_the_local_budget_is_reported_as_a_failed_search() -> None:
    """The provider job keeps running; only our wait gave up."""
    provider = Provider(running_polls=999)
    collector, _ = collector_for(
        provider, polling=PollingPolicy(interval_seconds=1.0, timeout_seconds=3.0)
    )

    with pytest.raises(ResearchCollectionError) as excinfo:
        collector.search("urban freight")

    assert "was not cancelled" in str(excinfo.value)
    assert excinfo.value.query == "urban freight"


# -- the failure boundary ---------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_every_provider_failure_becomes_a_research_collection_error(
    status: int,
) -> None:
    """No Bright Data exception type may reach the research core."""
    provider = Provider(trigger_status=status)
    collector, _ = collector_for(provider)

    with pytest.raises(ResearchCollectionError) as excinfo:
        collector.search("urban freight")

    assert excinfo.value.query == "urban freight"
    assert excinfo.value.message


def test_a_dataset_failure_is_also_translated() -> None:
    provider = Provider(dataset_status=500)
    collector, _ = collector_for(provider)

    with pytest.raises(ResearchCollectionError):
        collector.search("urban freight")


def test_the_error_names_the_provider_failure_type_without_leaking_secrets() -> None:
    provider = Provider(trigger_status=401)
    collector, _ = collector_for(provider)

    with pytest.raises(ResearchCollectionError) as excinfo:
        collector.search("urban freight")

    message = str(excinfo.value)
    assert "BrightDataAuthenticationError" in message
    assert "test-token-do-not-log" not in message


def test_the_adapter_is_usable_where_a_research_collector_is_expected() -> None:
    """Structural, because ResearchCollector is a Protocol, not a base class.

    Asserted by passing it to a function annotated with the port: if the
    signature drifted, this stops type-checking and stops working, and
    the returned result is the port's own type.
    """
    provider = Provider()
    collector, _ = collector_for(provider)

    def run_one(port: ResearchCollector, query: str) -> ResearchCollectionResult:
        return port.search(query)

    result = run_one(collector, "urban freight")

    assert isinstance(result, ResearchCollectionResult)
    assert result.query == "urban freight"
    assert result.provider_job_id == JOB_ID
