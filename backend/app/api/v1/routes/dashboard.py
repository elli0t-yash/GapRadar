"""One aggregated read for the frontend's landing view."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.deps import DbSession
from app.api.v1.views import collector_reliability, incident_counts, latest_run
from app.opportunity_engine.service import (
    count_signals,
    count_trusted_signals,
    list_opportunities,
)
from app.schemas.dashboard import (
    DashboardPipeline,
    DashboardRead,
    DashboardRecallGuard,
    DashboardSignals,
)
from app.schemas.reliability import worst_state

router = APIRouter(tags=["dashboard"])

MAX_TOP_OPPORTUNITIES = 50


@router.get("/dashboard", response_model=DashboardRead)
def get_dashboard(
    session: DbSession,
    top: Annotated[int, Query(ge=1, le=MAX_TOP_OPPORTUNITIES)] = 5,
) -> DashboardRead:
    """Pipeline state, RecallGuard state, signal counts, top opportunities.

    An empty database yields an empty dashboard -- HEALTHY (no active
    incident is exactly what healthy means), null timestamps, zero
    counts, and no opportunities. Nothing is filled in with a sample.
    """
    collectors = collector_reliability(session)
    state = worst_state([view.state for view in collectors])
    active_count, recovered_count = incident_counts(session)
    run = latest_run(session)

    # The headline incident is the one behind the headline state, so the
    # UI's banner and its detail panel always agree.
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
        # Trusted-only by construction: list_opportunities excludes
        # every collector with an active incident, so a degraded system
        # shows an empty list rather than stale data with a warning.
        top_opportunities=list_opportunities(session, limit=top),
    )
