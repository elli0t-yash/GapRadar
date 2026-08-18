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
