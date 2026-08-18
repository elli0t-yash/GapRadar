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

from pydantic import BaseModel, ConfigDict

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
