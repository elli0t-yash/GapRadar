"""Compose GapRadar's persisted product overview without side effects."""

from sqlalchemy.orm import Session

from app.opportunity_engine.service import (
    count_signals,
    count_trusted_signals,
    list_opportunities,
)
from app.recallguard.read_service import (
    collector_reliability,
    incident_counts,
    latest_run,
)
from app.schemas.dashboard import (
    DashboardPipeline,
    DashboardRead,
    DashboardRecallGuard,
    DashboardSignals,
)
from app.schemas.reliability import worst_state

DEFAULT_TOP_OPPORTUNITIES = 5
MAX_TOP_OPPORTUNITIES = 50


def get_dashboard_read(
    session: Session,
    *,
    top: int = DEFAULT_TOP_OPPORTUNITIES,
) -> DashboardRead:
    """Return the persisted dashboard view used by every interface."""
    collectors = collector_reliability(session)
    state = worst_state([view.state for view in collectors])
    active_count, recovered_count = incident_counts(session)
    run = latest_run(session)

    # The headline incident is the one behind the headline state, so every
    # interface presents a state and incident that agree.
    active = next(
        (
            view.active_incident
            for view in collectors
            if view.state is state and view.active_incident is not None
        ),
        None,
    )

    return DashboardRead(
        pipeline=DashboardPipeline(
            state=state,
            last_run_at=(run.completed_at or run.started_at or run.created_at)
            if run
            else None,
            last_run_id=run.id if run else None,
            last_run_status=run.status if run else None,
            last_record_count=run.record_count if run else None,
            last_collector_id=run.collector_id if run else None,
        ),
        recallguard=DashboardRecallGuard(
            state=state,
            active_incident=active,
            active_incident_count=active_count,
            recovered_incident_count=recovered_count,
        ),
        signals=DashboardSignals(
            total=count_signals(session),
            trusted=count_trusted_signals(session),
        ),
        # Trusted-only by construction: list_opportunities excludes every
        # collector with an active incident.
        top_opportunities=list_opportunities(session, limit=top),
    )
