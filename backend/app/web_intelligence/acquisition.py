"""The boundary between GapRadar and whatever runs a web search.

Mirrors app.research_intelligence.acquisition in shape and in discipline,
and is deliberately a SEPARATE PORT rather than a reuse of it.
ResearchCollector returns academic records with academic semantics --
abstracts, authors, arXiv identity; a web search returns ranked pages
with none of that. One protocol serving both would have to be the union
of two unrelated contracts, and every consumer would then have to ask
which half it was holding.

The reason this file exists at all: the whole demand and competitor
pipeline is testable with no network, because orchestration depends on
WebSearchProvider and never on an HTTP client.
"""

from collections.abc import Sequence
from typing import Protocol

from app.web_intelligence.schemas import (
    MAX_RESULTS_PER_QUERY,
    SearchLocale,
    WebIntelligenceRecord,
)


class WebSearchError(Exception):
    """One web search could not be carried out.

    THE MOST IMPORTANT TYPE IN THIS MODULE. A provider failure and a
    search that legitimately found nothing are opposite facts:

        []                 -> the engine answered, and there is nothing
        WebSearchError     -> the engine did not answer

    Collapsing them makes a broken provider look like an unserved market,
    which is precisely the conclusion GapRadar exists to get right.
    Implementations MUST raise rather than return an empty list to signal
    failure, and orchestration records the two differently.
    """

    def __init__(self, query: str, message: str) -> None:
        self.query = query
        self.message = message
        super().__init__(f"web search for {query!r} failed: {message}")


class WebSearchTimeoutError(WebSearchError):
    """The provider did not answer within the local budget.

    Distinct from a plain failure because the request may well have been
    served on the provider's side and billed; an operator reading a run
    needs to tell "we gave up waiting" from "it was refused".
    """


class WebSearchProviderUnavailableError(WebSearchError):
    """The provider could not be reached, or answered 5xx/401/403."""


class WebSearchInvalidResponseError(WebSearchError):
    """The provider answered with something this adapter cannot read.

    Not the same as a response containing no organic results -- that is a
    successful empty search. This is a body that is not JSON, or whose
    shape contradicts the documented contract.
    """


class WebSearchProvider(Protocol):
    """Runs ONE web search and returns its normalized organic results.

    A protocol, so the Bright Data SERP adapter, a replay provider for
    demos, and a test fake are interchangeable and none of them is named
    by orchestration.

    The contract, proven in the Bright Data pilot and deliberately narrow:

    - one query is ONE provider request for page 0;
    - organic results only -- no ads, no knowledge panels, no top stories;
    - at most `limit` records, and `limit` is capped at ten because that
      is one page;
    - NO NAVIGATION. The provider does not open the URLs it discovers.
      Deep reading of selected pages is a separate, later acquisition
      stage, and merging the two here would turn one search into eleven
      fetches.

    Returns [] for a successful search with no results. Raises
    WebSearchError -- never returns [] -- for any failure.
    """

    def search_web(
        self,
        query: str,
        *,
        limit: int = MAX_RESULTS_PER_QUERY,
        locale: SearchLocale | None = None,
    ) -> list[WebIntelligenceRecord]: ...


class SequenceWebSearchProvider:
    """Replays pre-fetched results. No network, no provider, no credentials.

    Not merely a test double -- it is how a saved SERP run is replayed
    into the pipeline for a demo or a backfill, which keeps the thing
    under test and the thing that runs the demo the same thing.

    A query with no entry raises WebSearchError, so a replay that does not
    cover the planned queries fails loudly instead of looking like a set
    of empty searches. A query mapped to an explicit empty list is a
    SUCCESSFUL empty search, which is how the two are exercised apart.
    """

    def __init__(
        self,
        results: dict[str, Sequence[WebIntelligenceRecord]],
        *,
        failures: dict[str, WebSearchError] | None = None,
    ) -> None:
        self._results = {query: list(records) for query, records in results.items()}
        self._failures = dict(failures or {})
        self.searched_queries: list[str] = []
        self.locales: list[SearchLocale | None] = []

    def search_web(
        self,
        query: str,
        *,
        limit: int = MAX_RESULTS_PER_QUERY,
        locale: SearchLocale | None = None,
    ) -> list[WebIntelligenceRecord]:
        self.searched_queries.append(query)
        self.locales.append(locale)
        if query in self._failures:
            raise self._failures[query]
        if query not in self._results:
            raise WebSearchError(query, "no recorded result for this query")
        return self._results[query][:limit]


__all__ = [
    "SequenceWebSearchProvider",
    "WebSearchError",
    "WebSearchInvalidResponseError",
    "WebSearchProvider",
    "WebSearchProviderUnavailableError",
    "WebSearchTimeoutError",
]
