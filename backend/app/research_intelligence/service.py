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
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    OpportunityResearchMatch,
    ResearchPaper,
    ResearchSearchResult,
    ResearchSearchRun,
    Signal,
)
from app.domain.enums import ResearchSource
from app.opportunity_engine.schemas import Opportunity
from app.research_intelligence.normalizer import (
    ResearchRecordRejectedError,
    normalize_arxiv_record,
)
from app.research_intelligence.schemas import (
    MarketContext,
    NormalizedResearchPaper,
    RawResearchRecord,
    RejectedResearchRecord,
    ResearchIngestionResult,
    ResearchIntelligence,
    ResearchPaperMatch,
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


# -- market context ---------------------------------------------------------


def market_context_from_signal(signal: Signal) -> MarketContext:
    """The research side's view of one market pain.

    Routed through the Opportunity read model rather than reading the
    Signal columns directly, so query generation and matching see exactly
    the wording the product surface shows -- including the same guarded
    reading of the untrusted metadata payload that `industry` comes from.
    Two readings of one row would drift, and the drift would be invisible.
    """
    opportunity = Opportunity.from_signal(signal)
    return MarketContext(
        signal_id=signal.id,
        problem=opportunity.problem,
        description=opportunity.description,
        industry=opportunity.industry,
    )


# -- read model -------------------------------------------------------------

# Characters of abstract shown in a card-sized preview, cut back to a
# word boundary so it never ends mid-word.
ABSTRACT_PREVIEW_CHARS = 280

DEFAULT_TOP_PAPERS = 10


def abstract_preview(abstract: str, *, limit: int = ABSTRACT_PREVIEW_CHARS) -> str:
    """A card-sized excerpt ending on a whole word."""
    if len(abstract) <= limit:
        return abstract
    cut = abstract[:limit].rsplit(" ", 1)[0].rstrip(",;:.")
    return f"{cut}\u2026"


def get_research_intelligence(
    session: Session,
    *,
    signal_id: uuid.UUID,
    top_papers: int = DEFAULT_TOP_PAPERS,
) -> ResearchIntelligence:
    """Everything persisted about the research behind one opportunity.

    READ ONLY. It never searches, never judges, and never contacts a
    provider -- an opportunity that has not been enriched returns an
    empty-but-valid result rather than triggering work on read.

    Trust is NOT decided here. The caller establishes that the
    opportunity is visible before asking, exactly as the Discover feed
    does; this function answers about whatever signal id it is given.
    """
    runs = list(
        session.execute(
            select(ResearchSearchRun)
            .where(ResearchSearchRun.signal_id == signal_id)
            .order_by(ResearchSearchRun.searched_at.desc())
        ).scalars()
    )
    queries: list[str] = []
    for run in runs:
        if run.query not in queries:
            queries.append(run.query)

    paper_count = (
        session.execute(
            select(func.count(func.distinct(ResearchSearchResult.research_paper_id)))
            .select_from(ResearchSearchResult)
            .join(
                ResearchSearchRun,
                ResearchSearchResult.research_search_run_id == ResearchSearchRun.id,
            )
            .where(ResearchSearchRun.signal_id == signal_id)
        ).scalar_one()
        or 0
    )

    matches = list(
        session.execute(
            select(OpportunityResearchMatch, ResearchPaper)
            .join(
                ResearchPaper,
                OpportunityResearchMatch.research_paper_id == ResearchPaper.id,
            )
            .where(OpportunityResearchMatch.signal_id == signal_id)
            .order_by(
                OpportunityResearchMatch.relevance_score.desc(),
                ResearchPaper.arxiv_id,
            )
        )
    )

    average = (
        round(sum(match.relevance_score for match, _ in matches) / len(matches), 2)
        if matches
        else None
    )

    return ResearchIntelligence(
        signal_id=signal_id,
        generated_queries=queries,
        paper_count=paper_count,
        matched_paper_count=len(matches),
        average_relevance_score=average,
        top_concepts=_top_concepts(match for match, _ in matches),
        top_papers=[
            _paper_match(match, paper) for match, paper in matches[:top_papers]
        ],
    )


def _paper_match(
    match: OpportunityResearchMatch, paper: ResearchPaper
) -> ResearchPaperMatch:
    return ResearchPaperMatch(
        research_paper_id=paper.id,
        arxiv_id=paper.arxiv_id,
        title=paper.title,
        abstract=paper.abstract,
        abstract_preview=abstract_preview(paper.abstract),
        authors=list(paper.authors or []),
        categories=list(paper.categories or []),
        published_at=paper.published_at,
        paper_url=paper.paper_url,
        pdf_url=paper.pdf_url,
        relevance_score=match.relevance_score,
        matched_concepts=list(match.matched_concepts or []),
        match_reason=match.match_reason,
        technical_readiness_score=match.technical_readiness_score,
    )


def _top_concepts(
    matches: Iterable[OpportunityResearchMatch], *, limit: int = 8
) -> list[str]:
    """Concepts ordered by how many matches mention them.

    Ties break alphabetically so the same data always renders the same
    list. Counted case-insensitively but reported in the casing the
    matcher used.
    """
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for match in matches:
        for concept in match.matched_concepts or []:
            key = concept.lower()
            counts[key] += 1
            display.setdefault(key, concept)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [display[key] for key, _ in ranked[:limit]]
