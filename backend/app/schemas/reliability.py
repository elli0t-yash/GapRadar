"""Frontend-facing views of RecallGuard's state.

Read models only. Nothing here judges reliability, opens an incident, or
decides what an incident means -- every value is copied from a persisted
ReliabilityIncident row or from RecallGuard's own computed state.

The lifecycle a UI renders

    HEALTHY -> DEGRADED -> HEALING -> VALIDATING -> VERIFYING -> RECOVERED

is exposed as the incident's real status plus a timeline derived from
timestamps and evidence that RecallGuard already wrote. No separate event
table is introduced, and no event is invented to fill a gap: if the
persisted evidence never recorded a transition, the timeline does not
show one.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import ReliabilityIncident
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
)

# How alarming each state is, worst first. Used to summarize several
# collectors as one headline state; it ranks presentation urgency and
# carries no reliability meaning of its own.
_STATE_SEVERITY: dict[ReliabilityState, int] = {
    ReliabilityState.HEALTHY: 0,
    ReliabilityState.VERIFYING: 1,
    ReliabilityState.VALIDATING: 2,
    ReliabilityState.HEALING: 3,
    ReliabilityState.DEGRADED: 4,
    ReliabilityState.MANUAL_REVIEW: 5,
}


def worst_state(states: list[ReliabilityState]) -> ReliabilityState:
    """The most alarming state in the set; HEALTHY when there is none."""
    return max(
        states,
        key=lambda state: _STATE_SEVERITY[state],
        default=ReliabilityState.HEALTHY,
    )


class IncidentEvent(BaseModel):
    """One lifecycle moment, derived from persisted evidence.

    `at`, `event`, and every optional field come from what RecallGuard
    already recorded on the incident. Nothing is synthesized.
    """

    model_config = ConfigDict(frozen=True)

    at: datetime
    event: str
    collector_run_id: uuid.UUID | None = None
    attempt: int | None = None
    detail: str | None = None


class ReliabilityIncidentSummary(BaseModel):
    """An incident at a glance."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    collector_id: uuid.UUID
    status: IncidentStatus
    classification: FailureClassification
    recommended_action: RecommendedAction
    repair_attempts: int
    detected_at: datetime
    recovered_at: datetime | None = None


class ReliabilityIncidentRead(ReliabilityIncidentSummary):
    """An incident in full, including the proof if it has one."""

    detection_run_id: uuid.UUID | None = None
    verification_run_id: uuid.UUID | None = None
    # Deterministic diagnosis written by RecallGuard. Passed through
    # as-is: it is the audit trail, not a display string.
    evidence: dict[str, Any] | None = None
    # Present only on a RECOVERED incident. Its absence is meaningful.
    recovery_proof: dict[str, Any] | None = None
    timeline: list[IncidentEvent] = []

    @classmethod
    def from_incident(cls, incident: ReliabilityIncident) -> "ReliabilityIncidentRead":
        return cls(
            id=incident.id,
            collector_id=incident.collector_id,
            status=incident.status,
            classification=incident.classification,
            recommended_action=incident.recommended_action,
            repair_attempts=incident.repair_attempts,
            detected_at=incident.detected_at,
            recovered_at=incident.recovered_at,
            detection_run_id=incident.detection_run_id,
            verification_run_id=incident.verification_run_id,
            evidence=incident.evidence,
            recovery_proof=incident.recovery_proof,
            timeline=build_timeline(incident),
        )


class CollectorReliabilityRead(BaseModel):
    """One collector's current reliability and its open incident, if any."""

    model_config = ConfigDict(frozen=True)

    collector_id: uuid.UUID
    name: str
    provider: str
    external_collector_id: str
    state: ReliabilityState
    active_incident: ReliabilityIncidentSummary | None = None
    last_run_id: uuid.UUID | None = None
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_record_count: int | None = None


class ReliabilityOverviewRead(BaseModel):
    """Every collector's reliability, plus one headline state."""

    model_config = ConfigDict(frozen=True)

    state: ReliabilityState
    collectors: list[CollectorReliabilityRead] = []
    active_incident_count: int = 0
    recovered_incident_count: int = 0


class DemoFieldHealth(BaseModel):
    """One field-level coverage comparison from the isolated replay fixture."""

    model_config = ConfigDict(frozen=True)

    field: str
    baseline_pct: float
    current_pct: float
    drop_pct: float | None = None
    status: str


class DemoVerificationResult(BaseModel):
    """One regression-guard assertion for a proposed repair."""

    model_config = ConfigDict(frozen=True)

    field: str
    before_pct: float
    after_pct: float
    status: str


class DemoRepairAttempt(BaseModel):
    """Persisted evidence for one deterministic repair proposal."""

    model_config = ConfigDict(frozen=True)

    attempt: int
    label: str
    status: str
    changes: list[str] = []
    verification: list[DemoVerificationResult] = []


class DemoFidelityProof(BaseModel):
    """The final, inspectable proof shown by the hackathon dashboard."""

    model_config = ConfigDict(frozen=True)

    schema_fidelity: str
    semantic_fidelity: str
    source_fidelity: str
    decision: str


class RecallGuardDemoRead(BaseModel):
    """Backend-owned presentation state for the isolated fixture replay.

    The status is intentionally a demo presentation state rather than a new
    ReliabilityState enum. Core RecallGuard correctly considers a recovered
    collector HEALTHY; this view preserves the stronger historical statement
    that this particular demo session reached SELF_HEALED.
    """

    model_config = ConfigDict(frozen=True)

    scenario: str
    mode: str
    session_id: uuid.UUID | None = None
    collector_id: uuid.UUID | None = None
    collector_name: str
    provider: str
    external_collector_id: str
    status: str
    core_status: str
    terminal: bool = False
    incident_id: uuid.UUID | None = None
    classification: str | None = None
    severity: str | None = None
    confidence: float | None = None
    recommended_action: str | None = None
    affected_fields: list[str] = []
    field_health: list[DemoFieldHealth] = []
    repair_attempts: list[DemoRepairAttempt] = []
    timeline: list[IncidentEvent] = []
    proof: DemoFidelityProof | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None


class LiveEvidenceCollector(BaseModel):
    """Identity of the one isolated real Bright Data healing collector."""

    model_config = ConfigDict(frozen=True)

    collector_id: uuid.UUID
    name: str
    provider: str
    external_collector_id: str


class LiveEvidenceInvalidRecord(BaseModel):
    """A bounded, display-safe projection of one persisted contract failure."""

    model_config = ConfigDict(frozen=True)

    index: int | None = None
    problem: str | None = None
    field: str
    value: float
    allowed_min: float
    allowed_max: float
    reason: str
    detail: str | None = None


class LiveEvidenceRun(BaseModel):
    """One real provider job and the counts persisted by its orchestrator."""

    model_config = ConfigDict(frozen=True)

    collector_run_id: uuid.UUID
    provider_job_id: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    fetched_record_count: int
    valid_record_count: int
    invalid_record_count: int
    accepted_record_count: int


class LiveEvidenceDetection(BaseModel):
    """RecallGuard's persisted verdict on the broken provider job."""

    model_config = ConfigDict(frozen=True)

    incident_id: uuid.UUID
    detected_at: datetime
    observed_record_count: int
    field: str
    classification: str
    severity: str | None = None
    confidence: float | None = None
    recommended_action: str


class LiveEvidenceRepairAttempt(BaseModel):
    """Provider candidate evidence retained on the real incident."""

    model_config = ConfigDict(frozen=True)

    attempt: int
    status: str
    provider_status: str | None = None
    has_diff: bool | None = None
    preview_records: int | None = None
    preview_valid_records: int | None = None
    preview_invalid_records: int | None = None
    deployed: bool = False
    patch_available: bool = False
    before_logic: str | None = None
    after_logic: str | None = None
    note: str | None = None


class LiveEvidenceVerificationSample(BaseModel):
    """A record accepted from the fresh real Bright Data verification run."""

    model_config = ConfigDict(frozen=True)

    problem: str
    tam_score: float


class LiveEvidenceFailedCheck(BaseModel):
    """One persisted regression check that blocked recovery."""

    model_config = ConfigDict(frozen=True)

    name: str
    expected: str | None = None
    observed: str | None = None
    detail: str | None = None


class LiveEvidenceVerification(BaseModel):
    """Fresh-provider-run contract result and RecallGuard's final verdict."""

    model_config = ConfigDict(frozen=True)

    run: LiveEvidenceRun
    samples: list[LiveEvidenceVerificationSample] = Field(default_factory=list)
    contract_validation: str
    regression_result: str
    failed_checks: list[LiveEvidenceFailedCheck] = Field(default_factory=list)
    final_decision: str
    final_status: str
    recovery_proof: dict[str, Any] | None = None


class LiveEvidenceAutomationStage(BaseModel):
    """Honest accounting of how the historical end-to-end flow was driven."""

    model_config = ConfigDict(frozen=True)

    stage: str
    automation: str
    result: str
    detail: str


class LiveBrightDataEvidenceRead(BaseModel):
    """Read-only proof from the isolated historical Bright Data experiment.

    This intentionally cannot masquerade as the deterministic fixture replay.
    Missing provider artifacts remain missing rather than being reconstructed
    from comments, tests, or fixture values.
    """

    model_config = ConfigDict(frozen=True)

    available: bool
    mode: str = "persisted_real_brightdata_run"
    live_trigger_safe: bool = False
    live_trigger_reason: str
    collector: LiveEvidenceCollector | None = None
    broken_run: LiveEvidenceRun | None = None
    invalid_records: list[LiveEvidenceInvalidRecord] = Field(default_factory=list)
    detection: LiveEvidenceDetection | None = None
    repair_attempts: list[LiveEvidenceRepairAttempt] = Field(default_factory=list)
    repair_patch_available: bool = False
    repair_patch_note: str
    verification: LiveEvidenceVerification | None = None
    automation: list[LiveEvidenceAutomationStage] = Field(default_factory=list)


def build_timeline(incident: ReliabilityIncident) -> list[IncidentEvent]:
    """Reconstruct the incident's lifecycle from what was persisted.

    Three real sources, in the order RecallGuard writes them: the
    detection timestamp on the row, the repeat failures accumulated in
    evidence["occurrences"], and the lifecycle transitions in
    evidence["events"]. A malformed or missing entry is skipped rather
    than guessed at -- an incomplete timeline is honest, a fabricated one
    is not.
    """
    evidence = incident.evidence or {}
    events: list[IncidentEvent] = [
        IncidentEvent(
            at=_utc(incident.detected_at),
            event="detected",
            collector_run_id=incident.detection_run_id,
            detail=incident.classification.value,
        )
    ]

    for occurrence in _entries(evidence, "occurrences"):
        at = _timestamp(occurrence.get("detected_at"))
        if at is None:
            continue
        events.append(
            IncidentEvent(
                at=at,
                event="degradation_observed",
                collector_run_id=_uuid(occurrence.get("collector_run_id")),
                detail=_text(occurrence.get("recommended_action")),
            )
        )

    for event in _entries(evidence, "events"):
        at = _timestamp(event.get("at"))
        name = _text(event.get("event"))
        if at is None or name is None:
            continue
        events.append(
            IncidentEvent(
                at=at,
                event=name,
                collector_run_id=_uuid(event.get("collector_run_id")),
                attempt=event.get("attempt")
                if isinstance(event.get("attempt"), int)
                else None,
                detail=_text(event.get("reason")) or _text(event.get("note")),
            )
        )

    events.sort(key=lambda entry: entry.at)
    return events


def _entries(evidence: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = evidence.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    """Normalize one of our own timestamps to an aware UTC datetime.

    Every timestamp on the timeline is written by GapRadar in UTC, but
    some database backends (SQLite, used by the test suite) hand a
    DateTime(timezone=True) column back without the offset -- and a
    timeline that mixes naive and aware values cannot be sorted at all.
    The same normalization RecallGuard applies to its own timestamps
    (app.recallguard.service._as_utc); provider-supplied timestamps are
    never treated this leniently.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
