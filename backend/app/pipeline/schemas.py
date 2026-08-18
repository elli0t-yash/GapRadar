"""What one pipeline run did, and how much of it can be trusted.

Every field here is either copied from an existing result object or
derived from RecallGuard's own verdict. Nothing in this module invents a
health, confidence, or recovery value: `trusted` is true only when
RecallGuard says the collector has no active incident AND the run that
produced the data passed every check.
"""

import enum
import uuid

from pydantic import BaseModel, ConfigDict

from app.collection.schemas import CollectionRunResult
from app.domain.enums import ReliabilityState
from app.recallguard.healing import HealingAttemptResult
from app.recallguard.schemas import ReliabilityEvaluation


class PipelineOutcome(str, enum.Enum):
    """How one pipeline run ended.

    Computed per invocation, never persisted -- the durable record is the
    CollectorRun plus the ReliabilityIncident RecallGuard maintains.
    """

    # The collection ran and passed every reliability check, and the
    # collector has no active incident.
    HEALTHY = "healthy"
    # A failing run (or a still-open incident). No repair was attempted,
    # either because the diagnosis is not repairable by healing the
    # scraper or because the incident is not in a state that may start
    # one.
    DEGRADED = "degraded"
    # A repair attempt ran and an independent fresh collection proved it.
    RECOVERED = "recovered"
    # A repair attempt ran and did not establish recovery.
    HEALING_FAILED = "healing_failed"
    # A provider repair is still in flight. GapRadar stopped waiting for
    # it -- a local timeout, not a provider failure -- and the next
    # invocation will resume the same repair rather than start another.
    HEALING_IN_PROGRESS = "healing_in_progress"
    # The incident is with a human now.
    MANUAL_REVIEW = "manual_review"
    # The collection never produced a CollectorRun to evaluate (a trigger
    # failure gets no collection id from the provider, so no run row can
    # exist). Fail-closed: nothing is trusted and no incident is invented.
    COLLECTION_UNEVALUABLE = "collection_unevaluable"


class CollectionFailure(BaseModel):
    """The collection error, as the run already records it.

    A summary for the caller; the authoritative copy stays on the
    CollectorRun's orchestration metadata (or, for a trigger failure,
    nowhere -- which is exactly why that case is reported separately).
    """

    model_config = ConfigDict(frozen=True)

    stage: str
    error: str
    message: str
    collector_run_id: uuid.UUID | None = None


class PipelineRunResult(BaseModel):
    """The result of one collect -> evaluate -> (heal) -> verify cycle."""

    model_config = ConfigDict(frozen=True)

    collector_id: uuid.UUID
    outcome: PipelineOutcome
    # The collector's CURRENT reliability after everything this run did.
    reliability_state: ReliabilityState
    # True only when this cycle produced data downstream may rely on.
    trusted: bool
    # The run this cycle executed, whether it succeeded or failed.
    collector_run_id: uuid.UUID | None = None
    # The run whose signals downstream may use -- the collection run when
    # it passed, or the independent verification run when a repair was
    # proven. None whenever `trusted` is false.
    trusted_collector_run_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    collection: CollectionRunResult | None = None
    collection_failure: CollectionFailure | None = None
    evaluation: ReliabilityEvaluation | None = None
    healing: HealingAttemptResult | None = None
    # Why no repair was attempted for a failing run. Present only when
    # healing was genuinely skipped, never as an excuse for a failure.
    healing_skipped_reason: str | None = None
