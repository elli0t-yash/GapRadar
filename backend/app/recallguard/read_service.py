"""Pure persisted reads for RecallGuard interfaces."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident
from app.domain.enums import IncidentStatus
from app.recallguard.service import active_incident, collector_reliability_state
from app.schemas.reliability import (
    CollectorReliabilityRead,
    ReliabilityIncidentRead,
    ReliabilityIncidentSummary,
    ReliabilityOverviewRead,
    worst_state,
)

DEFAULT_INCIDENT_LIMIT = 50
MAX_INCIDENT_LIMIT = 200


def collector_reliability(session: Session) -> list[CollectorReliabilityRead]:
    """Read every collector's persisted run and current RecallGuard state."""
    collectors = list(
        session.execute(select(Collector).order_by(Collector.name)).scalars()
    )
    views: list[CollectorReliabilityRead] = []
    for collector in collectors:
        incident = active_incident(session, collector_id=collector.id)
        run = latest_run(session, collector_id=collector.id)
        views.append(
            CollectorReliabilityRead(
                collector_id=collector.id,
                name=collector.name,
                provider=collector.provider,
                external_collector_id=collector.external_collector_id,
                state=collector_reliability_state(session, collector_id=collector.id),
                active_incident=(
                    ReliabilityIncidentSummary.model_validate(incident)
                    if incident
                    else None
                ),
                last_run_id=run.id if run else None,
                last_run_at=(run.completed_at or run.started_at or run.created_at)
                if run
                else None,
                last_run_status=run.status.value if run else None,
                last_record_count=run.record_count if run else None,
            )
        )
    return views


def latest_run(
    session: Session,
    *,
    collector_id: uuid.UUID | None = None,
) -> CollectorRun | None:
    """Read the most recently started run overall or for one collector."""
    query = select(CollectorRun).order_by(
        CollectorRun.started_at.desc(),
        CollectorRun.created_at.desc(),
    )
    if collector_id is not None:
        query = query.where(CollectorRun.collector_id == collector_id)
    return session.execute(query.limit(1)).scalar()


def incident_counts(session: Session) -> tuple[int, int]:
    """Return active and recovered incident counts from persisted rows."""
    recovered = session.execute(
        select(func.count())
        .select_from(ReliabilityIncident)
        .where(ReliabilityIncident.status == IncidentStatus.RECOVERED)
    ).scalar_one()
    total = session.execute(
        select(func.count()).select_from(ReliabilityIncident)
    ).scalar_one()
    return total - recovered, recovered


def get_reliability_overview(session: Session) -> ReliabilityOverviewRead:
    """Read the current persisted reliability state for all collectors."""
    collectors = collector_reliability(session)
    active, recovered = incident_counts(session)
    return ReliabilityOverviewRead(
        state=worst_state([view.state for view in collectors]),
        collectors=collectors,
        active_incident_count=active,
        recovered_incident_count=recovered,
    )


def list_reliability_incidents(
    session: Session,
    *,
    collector_id: uuid.UUID | None = None,
    status: IncidentStatus | None = None,
    limit: int = DEFAULT_INCIDENT_LIMIT,
) -> list[ReliabilityIncidentSummary]:
    """Read incidents most-recently detected first."""
    query = select(ReliabilityIncident).order_by(ReliabilityIncident.detected_at.desc())
    if collector_id is not None:
        query = query.where(ReliabilityIncident.collector_id == collector_id)
    if status is not None:
        query = query.where(ReliabilityIncident.status == status)
    incidents = session.execute(query.limit(limit)).scalars()
    return [ReliabilityIncidentSummary.model_validate(row) for row in incidents]


def get_reliability_incident(
    session: Session,
    *,
    incident_id: uuid.UUID,
) -> ReliabilityIncidentRead | None:
    """Read one incident with its persisted evidence and derived timeline."""
    incident = session.get(ReliabilityIncident, incident_id)
    return None if incident is None else ReliabilityIncidentRead.from_incident(incident)
