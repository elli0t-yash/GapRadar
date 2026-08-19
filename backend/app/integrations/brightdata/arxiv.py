"""Bright Data adapter for the arXiv research collector.

The concrete ResearchCollector: it turns a research query into an arXiv
search URL, runs Bright Data's published collector against it, waits for
the job, and hands the raw rows back.

WHY IT LIVES HERE. `app.research_intelligence` defines the port
(ResearchCollector) and must never import a provider -- that is what lets
query generation, candidate ranking and matching be tested with no
network. This module is the adapter, so the dependency points inward:
integrations knows about the research contracts, and the research core
knows nothing about Bright Data.

It performs NO normalization. Rows come back exactly as the provider sent
them and are validated by app.research_intelligence.normalizer, which
stays the single place that decides what a valid paper is.

The collector itself -- selectors, navigation, dynamic query handling --
is owned and frozen elsewhere. Nothing here edits it; this only submits
URLs to it.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from app.collection.schemas import DEFAULT_POLLING_POLICY, PollingPolicy
from app.integrations.brightdata.client import BrightDataClient
from app.integrations.brightdata.errors import BrightDataError
from app.integrations.brightdata.schemas import CollectorRunStatus
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollectionResult,
)

logger = logging.getLogger(__name__)

# The published, frozen arXiv research collector.
ARXIV_COLLECTOR_ID = "c_msz192rbcrk2ki1vj"

ARXIV_SEARCH_ENDPOINT = "https://arxiv.org/search/"

# Result cap the collector was validated against: the first page only,
# with no search pagination. Raising it here would submit a URL the
# collector has never been run against, so it is fixed rather than tuned.
ARXIV_RESULT_SIZE = 15

# Everything except `query`, in the order the validated URL uses.
_ARXIV_SEARCH_PARAMS: tuple[tuple[str, str], ...] = (
    ("searchtype", "all"),
    ("abstracts", "show"),
    ("order", "-announced_date_first"),
    ("size", str(ARXIV_RESULT_SIZE)),
)


def build_arxiv_search_url(query: str) -> str:
    """The arXiv search URL for one research query.

    Percent-encoded (`%20` for a space) rather than form-encoded (`+`),
    matching the URL shape the collector was validated against. The
    collector reads the value back with URLSearchParams, which decodes
    both, but there is no reason to hand it a shape nobody has tested.

    A blank query is refused here rather than submitted: it would produce
    a valid URL returning arXiv's entire recent listing, which is a real
    provider run spent on nothing.
    """
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("research query must not be blank")
    params = (("query", normalized), *_ARXIV_SEARCH_PARAMS)
    return f"{ARXIV_SEARCH_ENDPOINT}?{urlencode(params, quote_via=quote)}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BrightDataArxivCollector:
    """Runs one arXiv research search through Bright Data.

    SYNCHRONOUS, AND DELIBERATELY NOT THE FINAL ARCHITECTURE. `search()`
    blocks until the provider job finishes or the local budget runs out.
    That is acceptable for a controlled, operator-driven enrichment of a
    single opportunity, and it mirrors what app.collection.service
    already does for the market side. It is NOT suitable for enriching
    the whole corpus, and it is not a durable worker: a process that dies
    mid-wait loses the wait, though never the provider job. Replacing it
    with a reconciler that resumes by provider job id is a separate piece
    of work, and nothing outside this class needs to change for it.

    The polling budget is GapRadar's own and is never sent to Bright
    Data: passing a deadline to the trigger endpoint once terminated a
    real production run mid-collection.
    """

    def __init__(
        self,
        client: BrightDataClient,
        *,
        collector_id: str = ARXIV_COLLECTOR_ID,
        polling: PollingPolicy = DEFAULT_POLLING_POLICY,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._collector_id = collector_id
        self._polling = polling
        self._now = now
        self._sleep = sleep if sleep is not None else time.sleep

    def search(self, query: str) -> ResearchCollectionResult:
        """Run one research search and return its raw records.

        Every provider failure is translated into ResearchCollectionError
        so no Bright Data exception type reaches the research core --
        orchestration catches one error class and keeps going with the
        queries that worked.

        A completed job that returned nothing is a SUCCESS with
        `records=[]`, not an error. "arXiv has no papers for this query"
        and "the provider could not run the search" are different facts,
        and a caller that cannot tell them apart cannot tell a narrow
        topic from a broken collector.
        """
        try:
            url = build_arxiv_search_url(query)
        except ValueError as exc:
            raise ResearchCollectionError(query, str(exc)) from exc

        started_at = self._now()
        try:
            execution = self._client.trigger_collector_run(
                self._collector_id, [{"url": url}]
            )
            provider_job_id = execution.external_run_id
            logger.info(
                "research_search_triggered",
                extra={
                    "query": query,
                    "provider_job_id": provider_job_id,
                    "collector_id": self._collector_id,
                },
            )
            self._await_completion(query, provider_job_id)
            output = self._client.get_collector_output(provider_job_id)
        except BrightDataError as exc:
            # The provider's own message is preserved -- BrightDataError
            # guarantees it carries no credential -- and the type name is
            # kept so a timeout stays distinguishable from a 404 in logs.
            raise ResearchCollectionError(
                query, f"{type(exc).__name__}: {exc}"
            ) from exc

        completed_at = self._now()
        logger.info(
            "research_search_completed",
            extra={
                "query": query,
                "provider_job_id": provider_job_id,
                "record_count": len(output.records),
                "duration_seconds": round(
                    (completed_at - started_at).total_seconds(), 2
                ),
            },
        )
        return ResearchCollectionResult(
            query=query,
            records=output.records,
            provider_job_id=provider_job_id,
            searched_at=completed_at,
        )

    def _await_completion(self, query: str, provider_job_id: str) -> None:
        """Poll until the job is done, or until local patience runs out.

        Mirrors app.collection.service._poll_until_complete: the interval
        and budget come from PollingPolicy, the wait goes through the
        injected sleeper so tests never actually wait, and exhausting the
        budget is reported as a failure of THIS search rather than of the
        provider -- the job keeps running on Bright Data's side either
        way.
        """
        deadline = self._now() + timedelta(seconds=self._polling.timeout_seconds)
        polls = 0
        while True:
            polls += 1
            execution = self._client.get_collector_run_status(provider_job_id)
            if execution.status is CollectorRunStatus.SUCCEEDED:
                return

            if self._now() >= deadline:
                raise ResearchCollectionError(
                    query,
                    f"local wait of {self._polling.timeout_seconds}s elapsed "
                    f"while job {provider_job_id!r} was still running after "
                    f"{polls} poll(s); the provider job was not cancelled",
                )
            logger.info(
                "research_search_polling",
                extra={
                    "query": query,
                    "provider_job_id": provider_job_id,
                    "polls": polls,
                },
            )
            self._sleep(self._polling.interval_seconds)
