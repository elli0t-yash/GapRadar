"""Bright Data SERP API adapter. HTTP only -- never the CLI.

The concrete WebSearchProvider: it turns a query into one Google search
URL, submits it to Bright Data's SERP API over HTTPS, and hands back
normalized organic results.

WHY IT LIVES HERE. app.web_intelligence defines the port
(WebSearchProvider) and must never import a provider -- that is what lets
planning, classification and orchestration be tested with no network.
This module is the adapter, so the dependency points inward.

NO SUBPROCESS, EVER. The R&D pilot validated the provider through
`brightdata search` in CLI 0.3.5, which was the right tool for proving a
product choice and the wrong one for a backend: it would need the CLI
installed in the container, its own credential store, and a shell for
every search. The CLI's `--pretty` mode was just a request for parsed
JSON, which the HTTP API exposes directly as `data_format: parsed_light`.

Verified against docs.brightdata.com/api-reference/rest-api/serp/serp-api
and docs.brightdata.com/scraping-automation/serp-api/introduction:

    POST https://api.brightdata.com/request
    Authorization: Bearer <token>
    {"zone": ..., "url": "https://www.google.com/search?q=...",
     "format": "raw", "data_format": "parsed_light"}

`parsed_light` is chosen deliberately over the fuller parsed format: it
returns the top ten ORGANIC results only, excluding ads and knowledge
panels, at lower latency. That is exactly this phase's contract, and it
means the exclusion of paid placements is the provider's guarantee rather
than a filter of ours that could regress.

It performs NO normalization of its own. Rows are handed to
app.web_intelligence.normalization, which stays the single place that
decides what a usable web record is.
"""

import logging
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings
from app.web_intelligence.acquisition import (
    WebSearchError,
    WebSearchInvalidResponseError,
    WebSearchProviderUnavailableError,
    WebSearchTimeoutError,
)
from app.web_intelligence.normalization import (
    normalize_organic_results,
    validate_limit,
    validate_query,
)
from app.web_intelligence.schemas import (
    DEFAULT_LOCALE,
    MAX_RESULTS_PER_QUERY,
    SearchLocale,
    WebIntelligenceRecord,
)

logger = logging.getLogger(__name__)

SERP_REQUEST_PATH = "/request"

# Google web search. Page 0 only: no `start` parameter is ever sent, so
# there is exactly one request per query and no pagination is possible by
# accident.
GOOGLE_SEARCH_ENDPOINT = "https://www.google.com/search"

# What Bright Data calls this product in its own telemetry, recorded on
# every execution row so a future provider swap is visible in the data
# rather than only in the git history.
PROVIDER_NAME = "brightdata"
PROVIDER_PRODUCT = "serp_api"

# One SERP request is seconds, not minutes: the pilot measured 2.1s-15.4s
# across seven real requests, with the 15.4s case being a failure. 30s is
# generous enough for the slow tail and short enough that four concurrent
# searches cannot hold a run for long.
DEFAULT_TIMEOUT_SECONDS = 30.0


def build_google_search_url(
    query: str, *, limit: int, locale: SearchLocale
) -> str:
    """The Google search URL for one query. Page 0, nothing else.

    `gl` and `hl` are the locale; `num` asks for the requested count.
    There is deliberately no `start` and no `page`: their absence is what
    makes "one query is one request" a property of the code rather than a
    convention someone has to remember.
    """
    params = {
        "q": query,
        "gl": locale.country,
        "hl": locale.language,
        "num": str(limit),
    }
    return f"{GOOGLE_SEARCH_ENDPOINT}?{urlencode(params)}"


class BrightDataSerpWebSearchProvider:
    """WebSearchProvider backed by Bright Data's SERP API over HTTP.

    Reuses the backend's existing Bright Data credential configuration
    (Settings.BRIGHTDATA_API_KEY, Settings.BRIGHTDATA_BASE_URL) rather
    than introducing a second one. The only genuinely new setting is the
    SERP ZONE, which is a Bright Data product configuration and has no
    equivalent in the Scraper Studio client.

    Constructed with a transport in tests, exactly like BrightDataClient,
    so no test on this path can reach the network.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved = settings or get_settings()
        self._api_token = resolved.BRIGHTDATA_API_KEY
        self._zone = resolved.BRIGHTDATA_SERP_ZONE
        self._client = httpx.Client(
            base_url=resolved.BRIGHTDATA_BASE_URL,
            timeout=timeout,
            transport=transport,
        )
        # Observability, read by orchestration and persisted alongside the
        # execution record. Never a credential.
        self.provider = PROVIDER_NAME
        self.product = PROVIDER_PRODUCT

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def search_web(
        self,
        query: str,
        *,
        limit: int = MAX_RESULTS_PER_QUERY,
        locale: SearchLocale | None = None,
    ) -> list[WebIntelligenceRecord]:
        """One query, one request, page 0, organic only.

        Returns [] when the provider answered and matched nothing.
        RAISES for every failure -- transport, timeout, auth, 5xx,
        unreadable body -- because a broken provider must never be
        readable as an unserved market.

        Never opens a discovered URL. This method's entire network
        footprint is the single POST below.
        """
        clean_query = validate_query(query)
        bound = validate_limit(limit)
        resolved_locale = locale or DEFAULT_LOCALE

        payload = {
            "zone": self._zone,
            "url": build_google_search_url(
                clean_query, limit=bound, locale=resolved_locale
            ),
            # `raw` with `data_format: parsed_light` returns the parsed
            # JSON body directly. `format: json` would wrap it in an
            # envelope this adapter would then have to unwrap.
            "format": "raw",
            "data_format": "parsed_light",
            # Proxy exit country, kept consistent with the `gl` above so
            # the request and the search agree about where it came from.
            "country": resolved_locale.country,
        }

        response = self._post(clean_query, payload)
        body = self._read_json(clean_query, response)
        records = normalize_organic_results(
            body, query=clean_query, limit=bound
        )
        logger.info(
            "web_search_completed",
            extra={
                "provider": self.provider,
                "product": self.product,
                "locale": str(resolved_locale),
                "records": len(records),
            },
        )
        return records

    def _post(self, query: str, payload: dict[str, Any]) -> httpx.Response:
        """Submit the request, mapping every transport fault onto the port.

        The provider's own exception types stop here. Orchestration
        catches WebSearchError and nothing vendor-specific, which is what
        keeps a provider swap from touching it.
        """
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(
                SERP_REQUEST_PATH, headers=headers, json=payload
            )
        except httpx.TimeoutException as exc:
            raise WebSearchTimeoutError(
                query, "the search provider did not answer in time"
            ) from exc
        except httpx.TransportError as exc:
            raise WebSearchProviderUnavailableError(
                query, "the search provider could not be reached"
            ) from exc

        if response.status_code in (401, 403):
            # Deliberately does not echo the response body: an auth
            # failure's body is the likeliest place for a token to appear.
            raise WebSearchProviderUnavailableError(
                query,
                f"the search provider rejected our credentials "
                f"({response.status_code})",
            )
        if response.status_code >= 500:
            raise WebSearchProviderUnavailableError(
                query, f"the search provider returned {response.status_code}"
            )
        if response.status_code >= 400:
            raise WebSearchError(
                query, f"the search provider returned {response.status_code}"
            )
        return response

    def _read_json(self, query: str, response: httpx.Response) -> Any:
        """The parsed body, or a failure. Never a silent empty result."""
        try:
            return response.json()
        except ValueError as exc:
            raise WebSearchInvalidResponseError(
                query, "the search provider's response was not valid JSON"
            ) from exc


__all__ = [
    "PROVIDER_NAME",
    "PROVIDER_PRODUCT",
    "BrightDataSerpWebSearchProvider",
    "build_google_search_url",
]
