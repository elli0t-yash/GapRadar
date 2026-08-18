"""Collectors and their run history."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.v1.deps import DbSession
from app.db.models import Collector, CollectorRun
from app.exceptions import AppError
from app.schemas.collector import CollectorRead
from app.schemas.collector_run import CollectorRunRead

router = APIRouter(prefix="/collectors", tags=["collectors"])

MAX_RUNS = 200


@router.get("", response_model=list[CollectorRead])
def list_collectors(session: DbSession) -> list[Collector]:
    return list(session.execute(select(Collector).order_by(Collector.name)).scalars())


@router.get("/{collector_id}/runs", response_model=list[CollectorRunRead])
def list_collector_runs(
    collector_id: uuid.UUID,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = 50,
) -> list[CollectorRun]:
    """This collector's runs, newest first.

    Runs are execution history, not trust verdicts: a SUCCEEDED run here
    says Bright Data ran the collector and the dataset satisfied the
    source contract, nothing more. Reliability lives under /reliability.
    """
    if session.get(Collector, collector_id) is None:
        raise AppError(f"collector {collector_id} not found", status_code=404)

    return list(
        session.execute(
            select(CollectorRun)
            .where(CollectorRun.collector_id == collector_id)
            .order_by(CollectorRun.started_at.desc(), CollectorRun.created_at.desc())
            .limit(limit)
        ).scalars()
    )
