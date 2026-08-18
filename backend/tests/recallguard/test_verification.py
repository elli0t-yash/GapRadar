from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
    RunStatus,
)
from app.recallguard.errors import VerificationRunRejectedError
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    collector_reliability_state,
    evaluate_collector_run,
    register_repair_candidate,
    start_healing,
    verify_recovery,
)
from tests.recallguard.conftest import (
    DETECTED_AT,
    FakeClock,
    RunBuilder,
    invalid_record,
)

BASELINE = BaselineProfile(label="synthetic", record_count=100)
BEFORE_REPAIR = DETECTED_AT - timedelta(hours=1)
AFTER_REPAIR = DETECTED_AT + timedelta(hours=1)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def incident(
    db_session: Session, runs: RunBuilder, clock: FakeClock
) -> ReliabilityIncident:
    """A drifted collector with a repair candidate awaiting proof."""
    detection_run = runs.succeeded(record_count=0)
    evaluation = evaluate_collector_run(
        db_session, run=detection_run, baseline=BASELINE, now=clock
    )
    degraded = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert degraded is not None
    start_healing(db_session, degraded, now=clock)
    return register_repair_candidate(
        db_session, degraded, candidate={"note": "proposed selector fix"}, now=clock
    )


def fresh_healthy_run(runs: RunBuilder) -> CollectorRun:
    return runs.succeeded(record_count=100, started_at=AFTER_REPAIR)


# --- runs that cannot establish recovery -----------------------------------


def test_the_detection_run_cannot_verify_itself(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    detection_run = db_session.get(CollectorRun, incident.detection_run_id)
    assert detection_run is not None

    with pytest.raises(VerificationRunRejectedError):
        verify_recovery(
            db_session,
            incident,
            verification_run=detection_run,
            baseline=BASELINE,
            now=clock,
        )

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.VALIDATING
    assert incident.recovery_proof is None


def test_a_pre_repair_run_cannot_verify_recovery(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    # A collection that finished before the repair existed says nothing
    # about whether the repair worked.
    stale = runs.succeeded(record_count=100, started_at=BEFORE_REPAIR)

    with pytest.raises(VerificationRunRejectedError, match="did not start after"):
        verify_recovery(
            db_session, incident, verification_run=stale, baseline=BASELINE, now=clock
        )

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.VALIDATING


def test_another_collectors_run_cannot_verify_recovery(
    db_session: Session,
    incident: ReliabilityIncident,
    other_collector: Collector,
    clock: FakeClock,
) -> None:
    foreign = RunBuilder(db_session, other_collector).succeeded(
        record_count=100, started_at=AFTER_REPAIR
    )

    with pytest.raises(VerificationRunRejectedError, match="another collector"):
        verify_recovery(
            db_session, incident, verification_run=foreign, baseline=BASELINE, now=clock
        )


def test_a_failed_run_cannot_verify_recovery(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    failed = runs.failed("collection", started_at=AFTER_REPAIR)

    with pytest.raises(VerificationRunRejectedError, match="executed successfully"):
        verify_recovery(
            db_session, incident, verification_run=failed, baseline=BASELINE, now=clock
        )


def test_a_verification_run_cannot_be_reused(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    drifted = runs.succeeded(record_count=0, started_at=AFTER_REPAIR)
    verify_recovery(
        db_session, incident, verification_run=drifted, baseline=BASELINE, now=clock
    )
    start_healing(db_session, incident, now=clock)
    register_repair_candidate(db_session, incident, now=clock)

    with pytest.raises(VerificationRunRejectedError, match="already been used"):
        verify_recovery(
            db_session, incident, verification_run=drifted, baseline=BASELINE, now=clock
        )


# --- verification that fails -----------------------------------------------


def test_a_fresh_run_still_violating_the_source_contract_returns_to_degraded(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    still_broken = runs.source_validation_failed(
        invalid_records=[invalid_record(tam_score=60)], started_at=AFTER_REPAIR
    )
    still_broken.status = RunStatus.SUCCEEDED
    db_session.commit()

    evaluation = verify_recovery(
        db_session,
        incident,
        verification_run=still_broken,
        baseline=BASELINE,
        now=clock,
    )

    assert evaluation.passed is False
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovery_proof is None
    assert incident.recovered_at is None
    failure = incident.evidence["events"][-1]
    assert failure["event"] == "verification_failed"
    assert failure["collector_run_id"] == str(still_broken.id)
    assert failure["attempt"] == 1
    assert failure["sample_violations"][0]["raw"]["tam_score"] == 60


def test_a_fresh_run_with_zero_records_returns_to_degraded(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    empty = runs.succeeded(record_count=0, started_at=AFTER_REPAIR)

    evaluation = verify_recovery(
        db_session, incident, verification_run=empty, baseline=BASELINE, now=clock
    )

    assert evaluation.passed is False
    assert evaluation.state is ReliabilityState.DEGRADED
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovery_proof is None
    assert incident.repair_attempts == 1


def test_a_failed_verification_allows_another_attempt(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    verify_recovery(
        db_session,
        incident,
        verification_run=runs.succeeded(record_count=0, started_at=AFTER_REPAIR),
        baseline=BASELINE,
        now=clock,
    )

    healing = start_healing(db_session, incident, now=clock)

    assert healing.status is IncidentStatus.HEALING
    assert healing.repair_attempts == 2


# --- verification that passes ----------------------------------------------


def test_a_fresh_independent_healthy_run_recovers_the_incident(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    verification_run = fresh_healthy_run(runs)

    evaluation = verify_recovery(
        db_session,
        incident,
        verification_run=verification_run,
        baseline=BASELINE,
        now=clock,
    )

    assert evaluation.passed is True
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.RECOVERED
    assert incident.recovered_at is not None
    assert incident.verification_run_id == verification_run.id


def test_recovery_proof_names_both_runs_and_the_checks(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    detection_run_id = incident.detection_run_id
    verification_run = fresh_healthy_run(runs)

    verify_recovery(
        db_session,
        incident,
        verification_run=verification_run,
        baseline=BASELINE,
        now=clock,
    )

    db_session.refresh(incident)
    proof = incident.recovery_proof
    assert proof["result"] == "pass"
    assert proof["incident_id"] == str(incident.id)
    assert proof["collector_id"] == str(incident.collector_id)
    assert proof["detection_run_id"] == str(detection_run_id)
    assert proof["verification_run_id"] == str(verification_run.id)
    assert proof["classification"] == FailureClassification.EXTRACTION_DRIFT.value
    assert proof["repair_attempt"] == 1
    assert proof["verified_at"] is not None
    check_names = {check["name"] for check in proof["checks"]}
    assert {
        "execution",
        "transport_payload",
        "source_contract",
        "ingestion",
        "completeness",
    } <= check_names
    assert all(check["passed"] for check in proof["checks"])
    # The proof is evidence, not an opinion: no score, no confidence.
    assert "confidence" not in proof


# --- recovered vs healthy --------------------------------------------------


def test_a_recovered_incident_leaves_the_collector_healthy(
    db_session: Session,
    incident: ReliabilityIncident,
    collector: Collector,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    verify_recovery(
        db_session,
        incident,
        verification_run=fresh_healthy_run(runs),
        baseline=BASELINE,
        now=clock,
    )

    assert (
        collector_reliability_state(db_session, collector_id=collector.id)
        is ReliabilityState.HEALTHY
    )


def test_a_recovered_incident_is_kept_as_history(
    db_session: Session,
    incident: ReliabilityIncident,
    collector: Collector,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    # RECOVERED is a historical fact about an incident; HEALTHY is a
    # statement about now. The record is never deleted to produce the
    # latter.
    verify_recovery(
        db_session,
        incident,
        verification_run=fresh_healthy_run(runs),
        baseline=BASELINE,
        now=clock,
    )

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.RECOVERED
    assert incident.recovery_proof is not None
    assert (
        collector_reliability_state(db_session, collector_id=collector.id)
        is ReliabilityState.HEALTHY
    )


def test_a_new_failure_after_recovery_opens_a_separate_incident(
    db_session: Session,
    incident: ReliabilityIncident,
    runs: RunBuilder,
    clock: FakeClock,
) -> None:
    verify_recovery(
        db_session,
        incident,
        verification_run=fresh_healthy_run(runs),
        baseline=BASELINE,
        now=clock,
    )

    evaluation = evaluate_collector_run(
        db_session,
        run=runs.failed("payload", started_at=AFTER_REPAIR),
        baseline=BASELINE,
        now=clock,
    )

    assert evaluation.incident_id != incident.id
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.RECOVERED
