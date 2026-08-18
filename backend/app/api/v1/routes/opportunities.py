"""Trusted problem signals, ranked.

Only signals from a collector RecallGuard currently calls HEALTHY are
exposed. A degraded, healing, validating, verifying, or escalated
collector contributes nothing here, so bad data cannot reach the
frontend by way of this surface.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.deps import DbSession
from app.exceptions import AppError
from app.opportunity_engine.schemas import Opportunity
from app.opportunity_engine.service import (
    DEFAULT_LIMIT,
    get_opportunity,
    list_opportunities,
)
from app.research_intelligence.schemas import ResearchIntelligence
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
