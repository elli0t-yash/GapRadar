"""One aggregated read for the frontend's landing view."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.deps import DbSession
from app.dashboard.service import MAX_TOP_OPPORTUNITIES, get_dashboard_read
from app.schemas.dashboard import DashboardRead

router = APIRouter(tags=["dashboard"])

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
    return get_dashboard_read(session, top=top)
