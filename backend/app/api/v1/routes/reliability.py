"""RecallGuard's state and incident history, read-only.

Nothing on this surface can change an incident. Approving, rejecting,
escalating, and recovering all happen through the pipeline and the
healing lifecycle, where the trust rules are enforced -- exposing them as
HTTP verbs would make it possible to mark something recovered without a
fresh verified run.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.v1.deps import DbSession
from app.api.v1.views import collector_reliability, incident_counts
from app.db.models import ReliabilityIncident
from app.domain.enums import IncidentStatus
from app.exceptions import AppError
from app.schemas.reliability import (
    ReliabilityIncidentRead,
    ReliabilityIncidentSummary,
    ReliabilityOverviewRead,
    worst_state,
)

router = APIRouter(prefix="/reliability", tags=["reliability"])

MAX_INCIDENTS = 200


@router.get("", response_model=ReliabilityOverviewRead)
def get_reliability(session: DbSession) -> ReliabilityOverviewRead:
    collectors = collector_reliability(session)
    active, recovered = incident_counts(session)
    return ReliabilityOverviewRead(
        state=worst_state([view.state for view in collectors]),
        collectors=collectors,
        active_incident_count=active,
        recovered_incident_count=recovered,
    )


@router.get("/incidents", response_model=list[ReliabilityIncidentSummary])
def list_incidents(
    session: DbSession,
    collector_id: uuid.UUID | None = None,
    status: IncidentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_INCIDENTS)] = 50,
) -> list[ReliabilityIncident]:
    """Incidents, most recently detected first.

    A healthy collector has no row here at all -- "healthy" is the
    absence of an active incident, never an incident of its own.
    """
    query = select(ReliabilityIncident).order_by(ReliabilityIncident.detected_at.desc())
    if collector_id is not None:
        query = query.where(ReliabilityIncident.collector_id == collector_id)
    if status is not None:
        query = query.where(ReliabilityIncident.status == status)
    return list(session.execute(query.limit(limit)).scalars())


@router.get("/incidents/{incident_id}", response_model=ReliabilityIncidentRead)
def get_incident(incident_id: uuid.UUID, session: DbSession) -> ReliabilityIncidentRead:
    """One incident in full, with the timeline derived from its evidence."""
    incident = session.get(ReliabilityIncident, incident_id)
    if incident is None:
        raise AppError(f"incident {incident_id} not found", status_code=404)
    return ReliabilityIncidentRead.from_incident(incident)
