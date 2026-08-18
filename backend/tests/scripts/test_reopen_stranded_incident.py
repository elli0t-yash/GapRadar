"""The one-time reversal of the missing-preview escalation bug.

A script that writes to a production row earns tests: the guard is the
only thing standing between "reopen the one incident the bug stranded"
and "reopen an incident a human legitimately owns", and the difference is
four field values.

The fixture reproduces the live row the way the bug actually produced it
-- a real drift evaluation, a real attempt, then the escalation -- rather
than by assembling a ReliabilityIncident with the right columns set, so
the evidence the script appends to is the evidence RecallGuard writes.
"""

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, ReliabilityIncident
from app.domain.enums import FailureClassification, IncidentStatus, RecommendedAction
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import escalate, evaluate_collector_run, start_healing
from scripts.reopen_stranded_incident import (
    REOPEN_REASON,
    describe,
    refusal_reason,
    reopen,
)
from tests.recallguard.conftest import FakeClock, RunBuilder, invalid_record

BASELINE = BaselineProfile(label="fix_my_itch_healthy_v1", record_count=10)


@pytest.fixture
def stranded_incident(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> ReliabilityIncident:
    """Incident ae20c718 as the bug left it.

    Drift detected, attempt 1 started and spent, then escalated by the
    old preflight because the candidate arrived with no preview_result.
    """
    clock = FakeClock()
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=10
        ),
        baseline=BASELINE,
        now=clock,
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None

    start_healing(db_session, incident, now=clock)
    escalate(
        db_session,
        incident,
        reason="no preview_result to validate; refusing to approve a candidate unseen",
        now=clock,
    )

    assert incident.status is IncidentStatus.MANUAL_REVIEW
    assert incident.classification is FailureClassification.EXTRACTION_DRIFT
    assert incident.recommended_action is RecommendedAction.ESCALATE
    assert incident.repair_attempts == 1
    return incident


def snapshot(incident: ReliabilityIncident) -> dict[str, Any]:
    """Everything this script must leave exactly as it found it."""
    return {
        "collector_id": incident.collector_id,
        "detection_run_id": incident.detection_run_id,
        "verification_run_id": incident.verification_run_id,
        "classification": incident.classification,
        "repair_attempts": incident.repair_attempts,
        "detected_at": incident.detected_at,
        "recovered_at": incident.recovered_at,
        "recovery_proof": incident.recovery_proof,
    }


def reopen_events(incident: ReliabilityIncident) -> list[dict[str, Any]]:
    return [
        event
        for event in (incident.evidence or {}).get("events", [])
        if event["event"] == "incident_reopened"
    ]


# --- the guard -------------------------------------------------------------


def test_the_stranded_incident_is_recognized(
    stranded_incident: ReliabilityIncident,
) -> None:
    assert refusal_reason(stranded_incident) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Escalated for a legitimate reason: the budget ran out. This is
        # the case the guard exists to refuse.
        ("repair_attempts", 3),
        # A repair that has not been escalated at all needs no rescue.
        ("status", IncidentStatus.DEGRADED),
        ("status", IncidentStatus.HEALING),
        ("status", IncidentStatus.RECOVERED),
        # A different diagnosis was never subject to this bug: preflight
        # only ever runs for a repairable extraction failure.
        ("classification", FailureClassification.OUTAGE),
        ("recommended_action", RecommendedAction.REQUEST_HEAL),
    ],
)
def test_anything_but_the_exact_stranded_state_is_refused(
    db_session: Session,
    stranded_incident: ReliabilityIncident,
    field: str,
    value: Any,
) -> None:
    setattr(stranded_incident, field, value)
    db_session.commit()

    reason = refusal_reason(stranded_incident)

    assert reason is not None
    assert field in reason


def test_a_refusal_names_every_mismatched_field_at_once(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    stranded_incident.status = IncidentStatus.DEGRADED
    stranded_incident.repair_attempts = 3
    db_session.commit()

    reason = refusal_reason(stranded_incident)

    assert reason is not None
    assert "status" in reason
    assert "repair_attempts" in reason
    # The observed value is reported, not just the expectation.
    assert "found degraded" in reason
    assert "found 3" in reason


def test_an_already_reopened_incident_is_refused(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    """Re-running --apply is safe: the guard no longer recognizes the row."""
    reopen(db_session, stranded_incident)

    assert refusal_reason(stranded_incident) is not None


# --- the reversal ----------------------------------------------------------


def test_reopening_changes_exactly_the_two_intended_fields(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    before = snapshot(stranded_incident)

    reopen(db_session, stranded_incident)

    db_session.expire_all()
    incident = db_session.get(ReliabilityIncident, stranded_incident.id)
    assert incident is not None
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recommended_action is RecommendedAction.REQUEST_HEAL
    # Everything else survives untouched -- the attempt above all: it was
    # genuinely spent, so two remain rather than three.
    assert snapshot(incident) == before
    assert incident.repair_attempts == 1
    assert incident.classification is FailureClassification.EXTRACTION_DRIFT


def test_reopening_records_why_on_the_incident(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    clock = FakeClock()

    reopen(db_session, stranded_incident, now=clock)

    events = reopen_events(stranded_incident)
    assert len(events) == 1
    assert events[0] == {
        "event": "incident_reopened",
        "at": events[0]["at"],
        "reason": REOPEN_REASON,
        "previous_status": "manual_review",
        "previous_action": "escalate",
        "new_status": "degraded",
        "new_action": "request_heal",
        "repair_attempts": 1,
    }
    # A real timestamp, not a placeholder.
    assert datetime.fromisoformat(events[0]["at"]) == clock.current


def test_reopening_appends_to_the_existing_history(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    """The incident's own account of the bug is evidence, not clutter."""
    before = [event["event"] for event in stranded_incident.evidence["events"]]
    assert "escalated_to_manual_review" in before

    reopen(db_session, stranded_incident)

    after = [event["event"] for event in stranded_incident.evidence["events"]]
    assert after == [*before, "incident_reopened"]


def test_the_reopened_incident_is_repairable_again(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    """The point of the whole exercise: attempt 2 becomes possible.

    start_healing is the authority on that, so it is asked directly
    rather than inferred from the fields.
    """
    reopen(db_session, stranded_incident)

    start_healing(db_session, stranded_incident, now=FakeClock())

    assert stranded_incident.status is IncidentStatus.HEALING
    assert stranded_incident.repair_attempts == 2


# --- transactionality ------------------------------------------------------


def test_a_failure_leaves_neither_the_state_nor_the_evidence_behind(
    db_session: Session,
    stranded_incident: ReliabilityIncident,
) -> None:
    """State and evidence commit together or not at all.

    The status assignment happens in Python before the evidence is built,
    so a failure in between leaves a half-reopened object in the session.
    Only the rollback undoes that -- which is why this reads the object
    directly rather than expiring it first: expiring would discard the
    pending change on its own and pass whether or not reopen() rolls
    back.
    """
    before = describe(stranded_incident)

    def boom() -> datetime:
        raise RuntimeError("clock exploded mid-transaction")

    with pytest.raises(RuntimeError):
        reopen(db_session, stranded_incident, now=boom)

    assert describe(stranded_incident) == before
    assert stranded_incident.status is IncidentStatus.MANUAL_REVIEW
    assert stranded_incident.recommended_action is RecommendedAction.ESCALATE
    assert reopen_events(stranded_incident) == []
    # Nothing is left staged to leak into a later commit.
    assert not db_session.dirty

    # And the committed row agrees.
    db_session.expire_all()
    incident = db_session.get(ReliabilityIncident, stranded_incident.id)
    assert incident is not None
    assert describe(incident) == before
    assert reopen_events(incident) == []


def test_a_refused_incident_is_never_written_to(
    db_session: Session, stranded_incident: ReliabilityIncident
) -> None:
    """The guard is a read: asking it changes nothing."""
    stranded_incident.repair_attempts = 3
    db_session.commit()
    before = describe(stranded_incident)

    assert refusal_reason(stranded_incident) is not None

    db_session.expire_all()
    incident = db_session.get(ReliabilityIncident, stranded_incident.id)
    assert incident is not None
    assert describe(incident) == before
    assert reopen_events(incident) == []
