"""Read models shared by the reliability and dashboard routes.

Query-and-shape only: these functions read persisted rows and ask
RecallGuard for the current state. They never evaluate a run, open an
incident, or decide what any of it means.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident
from app.domain.enums import IncidentStatus
from app.recallguard.service import active_incident, collector_reliability_state
from app.schemas.reliability import (
    CollectorReliabilityRead,
    ReliabilityIncidentSummary,
)


def collector_reliability(session: Session) -> list[CollectorReliabilityRead]:
    """Every collector, with its current state and its open incident.

    The state comes from RecallGuard rather than from the last run's own
    status, which is the whole point: a collection that finished is not
    the same fact as a collector that can be trusted.
    """
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
    session: Session, *, collector_id: uuid.UUID | None = None
) -> CollectorRun | None:
    """The most recently started run, overall or for one collector."""
    query = select(CollectorRun).order_by(
        CollectorRun.started_at.desc(), CollectorRun.created_at.desc()
    )
    if collector_id is not None:
        query = query.where(CollectorRun.collector_id == collector_id)
    return session.execute(query.limit(1)).scalar()


def incident_counts(session: Session) -> tuple[int, int]:
    """(active, recovered) incident counts.

    RECOVERED incidents are counted separately and never deleted: a
    proven repair is a permanent record, not something erased to make the
    present look clean.
    """
    recovered = session.execute(
        select(func.count())
        .select_from(ReliabilityIncident)
        .where(ReliabilityIncident.status == IncidentStatus.RECOVERED)
    ).scalar_one()
    total = session.execute(
        select(func.count()).select_from(ReliabilityIncident)
    ).scalar_one()
    return total - recovered, recovered
