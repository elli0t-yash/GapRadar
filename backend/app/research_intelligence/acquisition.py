"""The boundary between GapRadar and whatever fetches research records.

GapRadar owns everything downstream of this file. Acquisition -- Bright
Data Scraper Studio, its collectors, selectors and job management -- is
owned separately, and the only thing the two sides agree on is the
protocol below.

That boundary is why the entire research pipeline is testable with no
network: orchestration depends on ResearchCollector, never on a client.
BrightDataClient is deliberately not imported here or in any module that
generates queries, ranks candidates, or judges matches.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.research_intelligence.schemas import RawResearchRecord


class ResearchCollectionError(Exception):
    """One research search could not be carried out.

    A collector raises this for a provider failure, a timeout, or an
    unusable response. Orchestration catches it per query, so one failed
    search never discards the results of the searches that worked.
    """

    def __init__(self, query: str, message: str) -> None:
        self.query = query
        self.message = message
        super().__init__(f"research search for {query!r} failed: {message}")


class ResearchCollectionResult(BaseModel):
    """What one research search returned.

    `records` are UNTRUSTED and unnormalized -- exactly the provider's
    output shape. Validation happens in
    app.research_intelligence.normalizer, not here, so a collector cannot
    accidentally become the thing that decides what a valid paper is.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    records: list[RawResearchRecord] = Field(default_factory=list)
    # The provider's own job/collection id, when it issues one. An
    # identifier, never a credential.
    provider_job_id: str | None = None
    # When the PROVIDER ran the search, which is not necessarily when
    # GapRadar ingested it. None means the collector could not say, and
    # ingestion falls back to its own clock rather than inventing one.
    searched_at: datetime | None = None


class ResearchCollector(Protocol):
    """Runs one research search and returns its raw records.

    A protocol, so Codex's Bright Data collector, a file-replay collector
    for demos, and a test fake are interchangeable and none of them is
    named by the orchestration.

    Implementations MUST raise ResearchCollectionError rather than
    returning an empty result to signal failure: "the search failed" and
    "the search found nothing" are different facts, and collapsing them
    would make a broken provider look like a quiet topic.
    """

    def search(self, query: str) -> ResearchCollectionResult: ...


class SequenceResearchCollector:
    """Replays pre-fetched records. No network, no provider, no credentials.

    Not a test double -- it is how a saved Bright Data run gets replayed
    into the pipeline for a demo or a backfill, which is exactly the
    handoff shape while the real collector is still being built. Tests
    use it too, which is the point: the thing under test is the same
    thing that runs the demo.

    A query with no entry raises ResearchCollectionError, so a replay
    that does not cover the generated queries fails loudly instead of
    looking like three empty searches.
    """

    def __init__(
        self,
        results: dict[str, Sequence[RawResearchRecord]],
        *,
        provider_job_id: str | None = None,
        searched_at: datetime | None = None,
    ) -> None:
        self._results = {query: list(records) for query, records in results.items()}
        self._provider_job_id = provider_job_id
        self._searched_at = searched_at
        self.searched_queries: list[str] = []

    def search(self, query: str) -> ResearchCollectionResult:
        self.searched_queries.append(query)
        if query not in self._results:
            raise ResearchCollectionError(query, "no recorded result for this query")
        return ResearchCollectionResult(
            query=query,
            records=self._results[query],
            provider_job_id=self._provider_job_id,
            searched_at=self._searched_at,
        )


__all__ = [
    "ResearchCollectionError",
    "ResearchCollectionResult",
    "ResearchCollector",
    "SequenceResearchCollector",
]
