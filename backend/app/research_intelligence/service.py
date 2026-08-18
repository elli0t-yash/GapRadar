"""Persist arXiv search results: upsert the papers, record the search.

The seam a future BrightDataArxivClient plugs into. Acquisition hands
this layer a plain list of records and the query that produced them;
nothing here knows how they were fetched, and nothing here calls a
provider. That separation is what lets the whole research side be tested
without a network.

Two things happen per call, and they are deliberately different in kind:

- PAPERS ARE UPSERTED. A paper is an entity keyed by arxiv_id. Finding it
  again is not a new paper.
- THE SEARCH IS APPENDED. Every call records a new ResearchSearchRun.
  Searching the same query twice really did happen twice, and collapsing
  that would destroy the provenance this table exists for.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ResearchPaper, ResearchSearchResult, ResearchSearchRun
from app.domain.enums import ResearchSource
from app.research_intelligence.normalizer import (
    ResearchRecordRejectedError,
    normalize_arxiv_record,
)
from app.research_intelligence.schemas import (
    NormalizedResearchPaper,
    RawResearchRecord,
    RejectedResearchRecord,
    ResearchIngestionResult,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def ingest_arxiv_search_results(
    session: Session,
    *,
    query: str,
    records: Sequence[RawResearchRecord],
    signal_id: uuid.UUID | None = None,
    searched_at: datetime | None = None,
    provider_job_id: str | None = None,
    source: ResearchSource = ResearchSource.ARXIV,
    commit: bool = True,
) -> ResearchIngestionResult:
    """Ingest one arXiv search's results and record how they were found.

    THE INGESTION ENTRY POINT. Whatever acquires records -- a Bright Data
    client, a saved JSON file, an operator script -- calls exactly this.

    Args:
        query: the search text that produced these records. Required and
            taken from the CALLER, never from the records: the collector
            currently pins its own `query` field
            (external/brightdata/arxiv/README.md), so trusting the record
            would silently mislabel every future dynamic query.
        records: raw records in the arXiv collector's output shape. Order
            is preserved as the search ranking.
        signal_id: the Signal behind the Opportunity that motivated this
            search, if there was one. None for an exploratory or operator
            search -- recorded honestly rather than refused.
        searched_at: when the provider actually ran the search. Defaults
            to now, which is right when ingestion follows acquisition
            immediately and wrong when a batch is replayed from a file,
            so a caller that knows better should say so.
        provider_job_id: the provider's job/collection id, when known.
        commit: transaction boundary, matching
            app.ingestion.service.ingest_collector_output. True commits
            once at the end; False flushes and leaves the transaction
            open for the caller to own.

    A record that fails validation is reported in `rejected` and skipped;
    it never aborts the batch and never becomes a partial paper.
    """
    search_run = ResearchSearchRun(
        signal_id=signal_id,
        source=source,
        query=query,
        searched_at=searched_at or _utcnow(),
        provider_job_id=provider_job_id,
    )
    session.add(search_run)
    session.flush()

    created = 0
    updated = 0
    unchanged = 0
    duplicates_in_batch = 0
    rejected: list[RejectedResearchRecord] = []
    paper_ids: list[uuid.UUID] = []
    seen_arxiv_ids: set[str] = set()

    for index, raw in enumerate(records):
        try:
            normalized = normalize_arxiv_record(raw)
        except ResearchRecordRejectedError as exc:
            rejected.append(
                RejectedResearchRecord(
                    index=index, reason=exc.reason, detail=exc.detail, raw=raw
                )
            )
            continue

        if normalized.arxiv_id in seen_arxiv_ids:
            # One search returned the same paper twice. Count it, and do
            # not attach a second result row -- the run/paper pair is
            # unique, and position would be a lie for the second copy.
            duplicates_in_batch += 1
            continue
        seen_arxiv_ids.add(normalized.arxiv_id)

        paper, outcome = _upsert_paper(session, normalized, source=source)
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

        paper_ids.append(paper.id)
        session.add(
            ResearchSearchResult(
                research_search_run_id=search_run.id,
                research_paper_id=paper.id,
                position=len(paper_ids) - 1,
            )
        )

    session.flush()
    if commit:
        session.commit()
        session.refresh(search_run)

    logger.info(
        "research_search_ingested",
        extra={
            "search_run_id": str(search_run.id),
            "signal_id": str(signal_id) if signal_id else None,
            "query": query,
            # Prefixed: a bare "created" collides with LogRecord.created.
            "papers_created": created,
            "papers_updated": updated,
            "papers_unchanged": unchanged,
            "duplicates_in_batch": duplicates_in_batch,
            "rejected": len(rejected),
        },
    )
    return ResearchIngestionResult(
        search_run_id=search_run.id,
        created=created,
        updated=updated,
        unchanged=unchanged,
        duplicates_in_batch=duplicates_in_batch,
        rejected=rejected,
        research_paper_ids=paper_ids,
    )


def get_paper_by_arxiv_id(session: Session, *, arxiv_id: str) -> ResearchPaper | None:
    """The stored paper for an arXiv id, or None."""
    return session.execute(
        select(ResearchPaper).where(ResearchPaper.arxiv_id == arxiv_id)
    ).scalar_one_or_none()


def _upsert_paper(
    session: Session,
    normalized: NormalizedResearchPaper,
    *,
    source: ResearchSource,
) -> tuple[ResearchPaper, str]:
    """Insert the paper, or update the existing row. Returns (paper, outcome).

    `outcome` is "created", "updated" or "unchanged". The three are
    distinguished rather than collapsed into "upserted" because
    re-ingesting an identical batch must be observably a no-op: reporting
    it as "updated" would make idempotency untestable and would churn
    updated_at on every search.

    The SELECT is an optimization, not the guarantee. Two concurrent
    ingestions can both miss and both insert; the unique constraint on
    arxiv_id fails the loser, and the SAVEPOINT rollback lets that record
    alone be retried as an update without poisoning the outer transaction
    or discarding the papers already accepted in this batch. The same
    pattern app.ingestion.service uses for Signals.
    """
    existing = get_paper_by_arxiv_id(session, arxiv_id=normalized.arxiv_id)
    if existing is not None:
        return existing, "updated" if _apply_updates(
            existing, normalized
        ) else "unchanged"

    paper = _build_paper(normalized, source=source)
    try:
        with session.begin_nested():
            session.add(paper)
            session.flush()
    except IntegrityError:
        session.expunge(paper)
        winner = get_paper_by_arxiv_id(session, arxiv_id=normalized.arxiv_id)
        if winner is None:
            # The constraint fired but nothing explains it -- a different
            # violation entirely. Raising beats returning a paper that
            # does not correspond to the record.
            raise
        logger.info(
            "research_paper_insert_lost_race",
            extra={"arxiv_id": normalized.arxiv_id, "paper_id": str(winner.id)},
        )
        return winner, "updated" if _apply_updates(winner, normalized) else "unchanged"

    return paper, "created"


def _build_paper(
    normalized: NormalizedResearchPaper, *, source: ResearchSource
) -> ResearchPaper:
    return ResearchPaper(
        arxiv_id=normalized.arxiv_id,
        source=source,
        title=normalized.title,
        abstract=normalized.abstract,
        authors=list(normalized.authors),
        categories=normalized.category_payload(),
        primary_category_code=normalized.primary_category_code,
        published_at=normalized.published_at,
        paper_url=normalized.paper_url,
        pdf_url=normalized.pdf_url,
    )


def _apply_updates(paper: ResearchPaper, normalized: NormalizedResearchPaper) -> bool:
    """Copy changed fields onto an existing paper. Returns True if anything moved.

    Assigned only when the value actually differs, so an unchanged
    re-ingestion leaves updated_at alone. `arxiv_id` and `source` are
    never touched -- they are the identity that found this row.
    """
    changes: dict[str, object] = {
        "title": normalized.title,
        "abstract": normalized.abstract,
        "authors": list(normalized.authors),
        "categories": normalized.category_payload(),
        "primary_category_code": normalized.primary_category_code,
        "published_at": normalized.published_at,
        "paper_url": normalized.paper_url,
        "pdf_url": normalized.pdf_url,
    }
    changed = False
    for field, value in changes.items():
        if getattr(paper, field) != value:
            setattr(paper, field, value)
            changed = True
    return changed
