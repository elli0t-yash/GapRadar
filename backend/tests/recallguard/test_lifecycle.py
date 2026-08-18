from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, ReliabilityIncident
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
)
from app.recallguard.errors import (
    IncidentTransitionError,
    RepairAttemptLimitExceededError,
)
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    MAX_AUTONOMOUS_REPAIR_ATTEMPTS,
    begin_validation,
    collector_reliability_state,
    evaluate_collector_run,
    register_repair_candidate,
    start_healing,
    verify_recovery,
)
from tests.recallguard.conftest import DETECTED_AT, FakeClock, RunBuilder

BASELINE = BaselineProfile(label="synthetic", record_count=100)
LATER = DETECTED_AT + timedelta(hours=1)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def degraded_incident(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> ReliabilityIncident:
    """An incident whose diagnosis calls for a scraper repair."""
    run = runs.succeeded(record_count=0)
    evaluation = evaluate_collector_run(
        db_session, run=run, baseline=BASELINE, now=clock
    )
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None
    return incident


def healing_incident(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> ReliabilityIncident:
    return start_healing(
        db_session, degraded_incident(db_session, runs, clock), now=clock
    )


def validating_incident(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> ReliabilityIncident:
    return register_repair_candidate(
        db_session,
        healing_incident(db_session, runs, clock),
        candidate={"provider": "brightdata", "note": "candidate registered by task 4"},
        now=clock,
    )


# --- guarded transitions ---------------------------------------------------


def test_degraded_moves_to_healing(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = degraded_incident(db_session, runs, clock)

    healing = start_healing(db_session, incident, now=clock)

    assert healing.status is IncidentStatus.HEALING
    assert healing.repair_attempts == 1
    assert healing.evidence["events"][-1]["event"] == "healing_started"


def test_healing_is_refused_for_a_provider_outage(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    # Healing the scraper cannot fix Bright Data being unavailable.
    evaluation = evaluate_collector_run(
        db_session, run=runs.failed("timeout"), baseline=BASELINE, now=clock
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None

    with pytest.raises(IncidentTransitionError):
        start_healing(db_session, incident, now=clock)

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.repair_attempts == 0


def test_healing_is_refused_for_an_internal_ingestion_failure(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    evaluation = evaluate_collector_run(
        db_session, run=runs.failed("ingestion"), baseline=BASELINE, now=clock
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None

    with pytest.raises(IncidentTransitionError):
        start_healing(db_session, incident, now=clock)


def test_repair_candidate_requires_healing_state(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = degraded_incident(db_session, runs, clock)

    with pytest.raises(IncidentTransitionError):
        register_repair_candidate(db_session, incident, now=clock)

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED


def test_repair_candidate_moves_healing_to_validating(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = healing_incident(db_session, runs, clock)

    validating = register_repair_candidate(
        db_session, incident, candidate={"diff": "selector updated"}, now=clock
    )

    assert validating.status is IncidentStatus.VALIDATING
    event = validating.evidence["events"][-1]
    assert event["event"] == "repair_candidate_registered"
    assert event["candidate"] == {"diff": "selector updated"}


def test_a_repair_candidate_alone_is_not_recovery(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    # The whole point: a proposed repair proves nothing about the result.
    incident = validating_incident(db_session, runs, clock)

    assert incident.status is IncidentStatus.VALIDATING
    assert incident.status is not IncidentStatus.RECOVERED
    assert incident.recovered_at is None
    assert incident.recovery_proof is None
    assert (
        collector_reliability_state(db_session, collector_id=incident.collector_id)
        is ReliabilityState.VALIDATING
    )


def test_healing_cannot_restart_while_validating(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = validating_incident(db_session, runs, clock)

    with pytest.raises(IncidentTransitionError):
        start_healing(db_session, incident, now=clock)


def test_verification_cannot_begin_before_a_repair_candidate(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = healing_incident(db_session, runs, clock)

    with pytest.raises(IncidentTransitionError):
        begin_validation(
            db_session,
            incident,
            verification_run=runs.succeeded(started_at=LATER),
            now=clock,
        )


def test_recovery_cannot_be_verified_from_degraded(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = degraded_incident(db_session, runs, clock)

    with pytest.raises(IncidentTransitionError):
        verify_recovery(
            db_session,
            incident,
            verification_run=runs.succeeded(started_at=LATER),
            baseline=BASELINE,
            now=clock,
        )


# --- autonomous attempt limit ----------------------------------------------


def test_three_autonomous_attempts_are_allowed(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = degraded_incident(db_session, runs, clock)

    for attempt in (1, 2, 3):
        healing = start_healing(db_session, incident, now=clock)
        assert healing.status is IncidentStatus.HEALING
        assert healing.repair_attempts == attempt
        # A failed verification returns the incident to DEGRADED, which
        # is what makes the next attempt possible.
        incident.status = IncidentStatus.DEGRADED
        db_session.commit()

    assert incident.repair_attempts == MAX_AUTONOMOUS_REPAIR_ATTEMPTS


def test_a_fourth_attempt_escalates_to_manual_review(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = degraded_incident(db_session, runs, clock)
    for _ in range(MAX_AUTONOMOUS_REPAIR_ATTEMPTS):
        start_healing(db_session, incident, now=clock)
        incident.status = IncidentStatus.DEGRADED
        db_session.commit()

    with pytest.raises(RepairAttemptLimitExceededError):
        start_healing(db_session, incident, now=clock)

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.MANUAL_REVIEW
    assert incident.recommended_action is RecommendedAction.ESCALATE
    assert incident.repair_attempts == MAX_AUTONOMOUS_REPAIR_ATTEMPTS
    assert (
        collector_reliability_state(db_session, collector_id=incident.collector_id)
        is ReliabilityState.MANUAL_REVIEW
    )


def test_manual_review_survives_further_failures(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> None:
    incident = degraded_incident(db_session, runs, clock)
    for _ in range(MAX_AUTONOMOUS_REPAIR_ATTEMPTS):
        start_healing(db_session, incident, now=clock)
        incident.status = IncidentStatus.DEGRADED
        db_session.commit()
    with pytest.raises(RepairAttemptLimitExceededError):
        start_healing(db_session, incident, now=clock)

    evaluate_collector_run(
        db_session, run=runs.failed("payload"), baseline=BASELINE, now=clock
    )

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.MANUAL_REVIEW
    assert incident.recommended_action is RecommendedAction.ESCALATE


# --- source absence --------------------------------------------------------


def test_source_absence_stays_degraded_and_is_never_auto_accepted(
    db_session: Session, collector: Collector, runs: RunBuilder, clock: FakeClock
) -> None:
    # SOURCE_ABSENCE is a deliberate human classification. Accepting the
    # new reality means updating the baseline on purpose -- RecallGuard
    # will not do that for you, and will not call the collector healthy.
    incident = degraded_incident(db_session, runs, clock)
    incident.classification = FailureClassification.SOURCE_ABSENCE
    incident.recommended_action = RecommendedAction.ACCEPT_SOURCE_CHANGE
    db_session.commit()

    with pytest.raises(IncidentTransitionError):
        start_healing(db_session, incident, now=clock)

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert (
        collector_reliability_state(db_session, collector_id=collector.id)
        is ReliabilityState.DEGRADED
    )
