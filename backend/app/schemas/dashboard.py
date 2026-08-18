"""One aggregated payload for the frontend's landing view.

Every number is counted from persisted rows and every state comes from
RecallGuard. There is no placeholder, no sample, and no default that
stands in for missing data: an empty database produces zeros, nulls, and
an empty opportunity list rather than a plausible-looking dashboard.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ReliabilityState, RunStatus
from app.opportunity_engine.schemas import Opportunity
from app.schemas.reliability import ReliabilityIncidentSummary


class DashboardPipeline(BaseModel):
    """The most recent collection, and how much it can be trusted.

    `state` is RecallGuard's headline verdict across every collector --
    healthy, degraded, healing, validating, verifying, or manual_review.
    It is never derived from whether the last run merely finished:
    Bright Data DONE is not RecallGuard HEALTHY.
    """

    model_config = ConfigDict(frozen=True)

    state: ReliabilityState
    last_run_at: datetime | None = None
    last_run_id: UUID | None = None
    last_run_status: RunStatus | None = None
    last_record_count: int | None = None
    last_collector_id: UUID | None = None


class DashboardRecallGuard(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: ReliabilityState
    active_incident: ReliabilityIncidentSummary | None = None
    active_incident_count: int = 0
    recovered_incident_count: int = 0


class DashboardSignals(BaseModel):
    """Signal counts, split by whether their collector is trusted now."""

    model_config = ConfigDict(frozen=True)

    total: int = 0
    trusted: int = 0


class DashboardRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    pipeline: DashboardPipeline
    recallguard: DashboardRecallGuard
    signals: DashboardSignals
    # Only signals from a currently healthy collector ever appear here.
    top_opportunities: list[Opportunity] = []
