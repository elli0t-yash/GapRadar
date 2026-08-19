"""Trusted problem signals, ranked.

Only signals from a collector RecallGuard currently calls HEALTHY are
exposed. A degraded, healing, validating, verifying, or escalated
collector contributes nothing here, so bad data cannot reach the
frontend by way of this surface.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status

from app.api.v1.deps import DbSession, EnrichmentScheduler
from app.db.models import Signal
from app.domain.enums import ResearchEnrichmentStatus
from app.exceptions import AppError
from app.opportunity_engine.schemas import Opportunity
from app.opportunity_engine.service import (
    DEFAULT_LIMIT,
    get_opportunity,
    list_opportunities,
)
from app.research_intelligence.enrichment import (
    latest_enrichment,
    reconcile_stale_enrichments,
    start_enrichment,
)
from app.research_intelligence.schemas import (
    ResearchEnrichmentAccepted,
    ResearchEnrichmentRead,
    ResearchIntelligence,
)
from app.research_intelligence.service import get_research_intelligence

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

MAX_LIMIT = 200


@router.get("", response_model=list[Opportunity])
def list_opportunity_signals(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[Opportunity]:
    return list_opportunities(session, limit=limit)


@router.get("/{signal_id}", response_model=Opportunity)
def get_opportunity_signal(signal_id: uuid.UUID, session: DbSession) -> Opportunity:
    """One trusted problem.

    404 covers both "no such signal" and "that signal's collector is not
    currently healthy": an untrusted signal is not available here, and
    saying so any more precisely would leak untrusted data's existence
    into a surface that is supposed to be trusted-only.
    """
    opportunity = get_opportunity(session, signal_id=signal_id)
    if opportunity is None:
        raise AppError(f"opportunity {signal_id} not found", status_code=404)
    return opportunity


@router.get("/{signal_id}/research", response_model=ResearchIntelligence)
def get_opportunity_research(
    signal_id: uuid.UUID, session: DbSession
) -> ResearchIntelligence:
    """The research GapRadar has connected to one trusted problem.

    READ ONLY, AND STRICTLY SO. This endpoint reads persisted rows and
    nothing else: no Bright Data call, no search, no matching, no
    enrichment. A GET that quietly triggered a provider run would make
    page loads cost money and would make an idempotent-looking request
    mutate the database. Enrichment is
    app.research_intelligence.orchestration.enrich_opportunity_with_research,
    invoked deliberately -- never as a side effect of someone reading.

    The trust check is the same one the Discover feed applies, and it is
    applied FIRST: an untrusted signal 404s here exactly as it does on
    /opportunities/{signal_id}. Without that, this route would be a
    backdoor to confirm the existence of -- and publish research about --
    a problem whose collector RecallGuard currently distrusts.

    404 therefore covers both "no such opportunity" and "that
    opportunity is not currently trusted", deliberately without
    distinguishing them. An enriched-but-empty result is a 200 with zero
    matches, which is a different fact and reads as one.
    """
    if get_opportunity(session, signal_id=signal_id) is None:
        raise AppError(f"opportunity {signal_id} not found", status_code=404)
    return get_research_intelligence(session, signal_id=signal_id)


@router.post(
    "/{signal_id}/research/enrich",
    response_model=ResearchEnrichmentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def enrich_opportunity_research(
    signal_id: uuid.UUID,
    session: DbSession,
    background: BackgroundTasks,
    schedule: EnrichmentScheduler,
) -> ResearchEnrichmentAccepted:
    """Ask GapRadar to find research for one trusted problem.

    THE ONLY PATH THAT SPENDS A PROVIDER CALL FOR RESEARCH, and it is only
    ever reached by an explicit user action. `GET /research` stays a pure
    read; nothing about opening an opportunity triggers this.

    202, not 200: nothing has been searched or judged when this returns.
    The work inside the request is a trust check, one lookup and one
    INSERT -- no Bright Data call, no LLM call, no waiting.

    Three answers, all 202, because in every one of them the caller's
    request ("analyse this") is satisfied:

    - `already_enriched` -- this opportunity already has persisted
      research. Nothing is started; re-running would spend real money to
      recompute what is already on disk. The client reads GET /research.
      A deliberate re-analysis is a separate capability and does not
      exist yet.
    - `already_running` -- a job is in flight. It is returned as-is and
      nothing new is scheduled, so a double-click, two tabs, or a
      re-rendered effect cannot buy a second set of searches.
    - neither -- a job was claimed and handed to the executor.

    Trust gating is identical to GET: an unknown or untrusted opportunity
    404s, so this is not a backdoor to spend provider calls on a problem
    whose collector RecallGuard currently distrusts.
    """
    opportunity = get_opportunity(session, signal_id=signal_id)
    if opportunity is None:
        raise AppError(f"opportunity {signal_id} not found", status_code=404)

    signal = session.get(Signal, signal_id)
    if signal is None:  # pragma: no cover - get_opportunity already proved it
        raise AppError(f"opportunity {signal_id} not found", status_code=404)

    # Already-enriched wins over claiming: the cheapest correct answer is
    # to tell the client to read what already exists.
    existing = get_research_intelligence(session, signal_id=signal_id)
    if existing.matched_paper_count > 0 or existing.generated_queries:
        current = latest_enrichment(session, signal_id=signal_id)
        return ResearchEnrichmentAccepted(
            enrichment_id=current.id if current else signal_id,
            signal_id=signal_id,
            status=(current.status if current else ResearchEnrichmentStatus.SUCCEEDED),
            already_running=bool(
                current
                and current.status
                in (
                    ResearchEnrichmentStatus.QUEUED,
                    ResearchEnrichmentStatus.RUNNING,
                )
            ),
            already_enriched=True,
        )

    run, already_running = start_enrichment(session, signal=signal)

    if not already_running:
        # Runs after the response is sent, in this process. Local only,
        # and deliberately not treated as durable -- see
        # app.research_intelligence.background.
        background.add_task(schedule, run.id)

    return ResearchEnrichmentAccepted(
        enrichment_id=run.id,
        signal_id=signal_id,
        status=run.status,
        already_running=already_running,
        already_enriched=False,
    )


@router.get(
    "/{signal_id}/research/enrichment", response_model=ResearchEnrichmentRead | None
)
def get_opportunity_research_enrichment(
    signal_id: uuid.UUID, session: DbSession
) -> ResearchEnrichmentRead | None:
    """Where this opportunity's most recent analysis has got to.

    Read-only, cheap, and safe to poll. Returns null when no analysis has
    ever been requested -- which is a different fact from a job that
    exists and is queued, and lets the client tell "never asked" from
    "asked and waiting" after a page reload.

    Same trust gate as everything else on this surface.

    Reconciles abandoned runs first. FastAPI BackgroundTasks die with the
    process, so a reload mid-enrichment strands a row as RUNNING -- and
    because the active-job index is what stops duplicate provider spend,
    a stranded row would block that opportunity from ever being enriched
    again. Ageing it out here means the client that is already polling is
    the thing that unsticks it, with no scheduler to own.
    """
    if get_opportunity(session, signal_id=signal_id) is None:
        raise AppError(f"opportunity {signal_id} not found", status_code=404)

    reconcile_stale_enrichments(session)
    run = latest_enrichment(session, signal_id=signal_id)
    return None if run is None else ResearchEnrichmentRead.model_validate(run)
