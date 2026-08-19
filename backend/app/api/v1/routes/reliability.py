"""RecallGuard's state and incident history.

The production surface is read-only. Approving, rejecting, escalating, and
recovering production incidents happen through the pipeline and healing
lifecycle, where the trust rules are enforced.

The only writes here live under /demo: they accept no collector id, target the
known isolated development collector, and advance a persisted fixture replay
through those same RecallGuard lifecycle functions. They cannot mark even that
demo recovered without a fresh verified run.
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
from app.recallguard.demo import (
    DemoIsolationError,
    DemoNotStartedError,
    advance_demo,
    read_demo,
    start_demo,
)
from app.recallguard.live_evidence import read_live_brightdata_evidence
from app.schemas.reliability import (
    LiveBrightDataEvidenceRead,
    RecallGuardDemoRead,
    ReliabilityIncidentRead,
    ReliabilityIncidentSummary,
    ReliabilityOverviewRead,
    worst_state,
)

router = APIRouter(prefix="/reliability", tags=["reliability"])

MAX_INCIDENTS = 200


@router.get("/live-evidence", response_model=LiveBrightDataEvidenceRead)
def get_live_brightdata_evidence(session: DbSession) -> LiveBrightDataEvidenceRead:
    """Persisted proof from the isolated real Bright Data healing experiment.

    This is intentionally read-only and never contacts or mutates the provider.
    The deterministic fixture replay remains available under ``/demo``.
    """
    return read_live_brightdata_evidence(session)


@router.get("/demo", response_model=RecallGuardDemoRead)
def get_recallguard_demo(session: DbSession) -> RecallGuardDemoRead:
    """Latest isolated fixture-replay state. This endpoint is read-only."""
    return read_demo(session)


@router.post("/demo/start", response_model=RecallGuardDemoRead)
def start_recallguard_demo(session: DbSession) -> RecallGuardDemoRead:
    """Start at HEALTHY, or safely resume an interrupted demo session."""
    try:
        return start_demo(session)
    except DemoIsolationError as exc:
        raise AppError(str(exc), status_code=409) from exc


@router.post("/demo/advance", response_model=RecallGuardDemoRead)
def advance_recallguard_demo(session: DbSession) -> RecallGuardDemoRead:
    """Advance one backend-persisted transition; never calls Bright Data."""
    try:
        return advance_demo(session)
    except DemoNotStartedError as exc:
        raise AppError(str(exc), status_code=409) from exc


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
