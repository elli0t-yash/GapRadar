"""Reading back what web discovery found. Writes nothing, calls nobody.

Pure persisted reads, in the same spirit as
app.research_intelligence.service.get_subject_research_intelligence: an
investigation that has never run returns an empty-but-valid collection
rather than triggering work, which is what makes these endpoints safe to
poll and safe to open.

Provenance is assembled here rather than stored on the evidence row. The
hit rows already know which searches returned a URL; denormalising that
onto the evidence would mean re-running a search could leave the two
disagreeing about how a page was found.
"""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    InvestigationCompetitor,
    InvestigationDemandEvidence,
    InvestigationWebSearchHit,
    InvestigationWebSearchRun,
)
from app.investigations.schemas import (
    CompetitorCollection,
    CompetitorRead,
    DemandEvidenceCollection,
    DemandEvidenceRead,
    WebEvidenceProvenance,
)

# How many pieces of evidence one read returns. Bounded here as well as
# at the route: a background caller that skipped FastAPI's validation
# must not be able to pull an unbounded list into memory.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _provenance(
    session: Session, *, investigation_id: uuid.UUID
) -> dict[str, WebEvidenceProvenance]:
    """Which searches found each URL, in ONE query rather than N.

    Built for the whole investigation at once because a per-row lookup
    would be one query per piece of evidence -- the classic N+1 that
    turns a fifty-item page into fifty-one round trips.
    """
    rows = session.execute(
        select(
            InvestigationWebSearchHit.url,
            InvestigationWebSearchRun.query,
            InvestigationWebSearchHit.position,
        )
        .join(
            InvestigationWebSearchRun,
            InvestigationWebSearchHit.investigation_web_search_run_id
            == InvestigationWebSearchRun.id,
        )
        .where(InvestigationWebSearchRun.investigation_id == investigation_id)
        .order_by(InvestigationWebSearchRun.created_at)
    ).all()

    queries: dict[str, list[str]] = defaultdict(list)
    positions: dict[str, int | None] = {}
    for url, query, position in rows:
        if query not in queries[url]:
            queries[url].append(query)
        if position is not None:
            best = positions.get(url)
            positions[url] = position if best is None else min(best, position)

    return {
        url: WebEvidenceProvenance(
            found_by_queries=found, best_position=positions.get(url)
        )
        for url, found in queries.items()
    }


def get_demand_evidence(
    session: Session, *, investigation_id: uuid.UUID, limit: int = DEFAULT_LIMIT
) -> DemandEvidenceCollection:
    """Everything judged about demand for one investigation.

    Ordered by relevance, then URL. The URL tiebreak is not meaningful
    ranking -- it exists so two pages on the same score have a stable
    order and a paged read cannot show one twice.

    `counts` covers EVERY classification stored, including CONTRADICTS.
    Reporting only the supporting ones would turn a read model into an
    argument.
    """
    bounded = max(1, min(limit, MAX_LIMIT))
    rows = list(
        session.execute(
            select(InvestigationDemandEvidence)
            .where(InvestigationDemandEvidence.investigation_id == investigation_id)
            .order_by(
                InvestigationDemandEvidence.relevance_score.desc(),
                InvestigationDemandEvidence.url,
            )
        ).scalars()
    )
    provenance = _provenance(session, investigation_id=investigation_id)

    counts: dict[str, int] = {}
    for row in rows:
        key = row.classification.value
        counts[key] = counts.get(key, 0) + 1

    return DemandEvidenceCollection(
        investigation_id=investigation_id,
        counts=counts,
        evidence=[
            DemandEvidenceRead(
                id=row.id,
                url=row.url,
                domain=row.domain,
                title=row.title,
                snippet=row.snippet,
                published_at=row.published_at,
                classification=row.classification,
                relevance_score=row.relevance_score,
                reason=row.reason,
                provenance=provenance.get(row.url, WebEvidenceProvenance()),
            )
            for row in rows[:bounded]
        ],
    )


def get_competitors(
    session: Session, *, investigation_id: uuid.UUID, limit: int = DEFAULT_LIMIT
) -> CompetitorCollection:
    """Everything judged about competition for one investigation."""
    bounded = max(1, min(limit, MAX_LIMIT))
    rows = list(
        session.execute(
            select(InvestigationCompetitor)
            .where(InvestigationCompetitor.investigation_id == investigation_id)
            .order_by(
                InvestigationCompetitor.relevance_score.desc(),
                InvestigationCompetitor.url,
            )
        ).scalars()
    )
    provenance = _provenance(session, investigation_id=investigation_id)

    counts: dict[str, int] = {}
    for row in rows:
        key = row.classification.value
        counts[key] = counts.get(key, 0) + 1

    return CompetitorCollection(
        investigation_id=investigation_id,
        counts=counts,
        competitors=[
            CompetitorRead(
                id=row.id,
                url=row.url,
                domain=row.domain,
                name=row.name,
                snippet=row.snippet,
                classification=row.classification,
                relevance_score=row.relevance_score,
                reason=row.reason,
                provenance=provenance.get(row.url, WebEvidenceProvenance()),
            )
            for row in rows[:bounded]
        ],
    )
