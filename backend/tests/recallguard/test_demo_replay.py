"""Deterministic replay of the isolated RecallGuard presentation scenario."""

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, ReliabilityIncident, Source
from app.domain.enums import (
    CollectorStatus,
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
)
from app.recallguard.demo import (
    DEMO_EXTERNAL_COLLECTOR_ID,
    DEMO_PROVIDER,
    DemoIsolationError,
    advance_demo,
    read_demo,
    start_demo,
)
from tests.recallguard.conftest import DETECTED_AT

REAL_HEALING_EXTERNAL_COLLECTOR_ID = "c_msya3ha629w2q9c62m"


def _advance(session: Session, count: int):
    state = read_demo(session)
    for _ in range(count):
        state = advance_demo(session)
    return state


def test_demo_detects_the_fixture_drift_with_real_recallguard_logic(
    db_session: Session,
) -> None:
    initial = start_demo(db_session)
    assert initial.status == "healthy"
    assert initial.incident_id is None

    degraded = advance_demo(db_session)

    assert degraded.status == "drift_detected"
    assert degraded.classification == FailureClassification.EXTRACTION_DRIFT.value
    assert degraded.recommended_action == RecommendedAction.REQUEST_HEAL.value
    assert degraded.severity == "high"
    assert degraded.confidence == 0.838
    assert degraded.affected_fields == ["hazard", "remedy"]
    coverage = {row.field: row for row in degraded.field_health}
    assert coverage["hazard"].drop_pct == 78
    assert coverage["remedy"].drop_pct == 97


def test_bad_repair_is_rejected_when_it_regresses_a_healthy_field(
    db_session: Session,
) -> None:
    start_demo(db_session)
    rejected = _advance(db_session, 4)

    assert rejected.status == "rejected"
    assert rejected.core_status == "degraded"
    assert rejected.repair_attempts[0].status == "rejected"
    checks = {item.field: item for item in rejected.repair_attempts[0].verification}
    assert checks["hazard"].status == "pass"
    assert checks["remedy"].status == "pass"
    assert checks["title"].before_pct == 100
    assert checks["title"].after_pct == 40
    assert checks["title"].status == "fail"
    incident = db_session.get(ReliabilityIncident, rejected.incident_id)
    assert incident is not None
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovery_proof is None
    failed = [
        event
        for event in incident.evidence["events"]
        if event["event"] == "healing_failed"
    ]
    assert failed[-1]["reason"] == "regression_guard_failed"


def test_good_repair_requires_a_fresh_run_and_produces_final_proof(
    db_session: Session,
) -> None:
    start_demo(db_session)
    recovered = _advance(db_session, 7)

    assert recovered.status == "self_healed"
    assert recovered.core_status == "healthy"
    assert recovered.terminal is True
    assert [attempt.status for attempt in recovered.repair_attempts] == [
        "rejected",
        "approved",
    ]
    assert recovered.proof is not None
    assert recovered.proof.schema_fidelity == "PASS"
    assert recovered.proof.semantic_fidelity == "PASS"
    assert recovered.proof.source_fidelity == "PASS"
    assert recovered.proof.decision == "APPROVE"
    assert all(row.current_pct >= row.baseline_pct for row in recovered.field_health)

    incident = db_session.get(ReliabilityIncident, recovered.incident_id)
    assert incident is not None
    assert incident.status is IncidentStatus.RECOVERED
    assert incident.repair_attempts == 2
    assert incident.verification_run_id is not None
    assert incident.verification_run_id != incident.detection_run_id
    assert incident.recovery_proof["result"] == "pass"
    assert incident.recovery_proof["demo_fidelity"]["decision"] == "APPROVE"


def test_completed_demo_can_start_again_without_deleting_its_proof(
    db_session: Session,
) -> None:
    first = start_demo(db_session)
    recovered = _advance(db_session, 7)
    second = start_demo(db_session)

    assert recovered.incident_id is not None
    assert second.status == "healthy"
    assert second.session_id != first.session_id
    assert second.incident_id is None
    historical = db_session.get(ReliabilityIncident, recovered.incident_id)
    assert historical is not None
    assert historical.status is IncidentStatus.RECOVERED
    assert historical.recovery_proof["demo_fidelity"]["decision"] == "APPROVE"


def test_real_healing_incident_survives_two_complete_demo_sessions_unchanged(
    db_session: Session,
    source: Source,
) -> None:
    real_collector = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id=REAL_HEALING_EXTERNAL_COLLECTOR_ID,
        name="gapradar-fix-my-itch-heal-demo",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(real_collector)
    db_session.flush()
    real_incident = ReliabilityIncident(
        collector_id=real_collector.id,
        status=IncidentStatus.MANUAL_REVIEW,
        classification=FailureClassification.EXTRACTION_DRIFT,
        recommended_action=RecommendedAction.ESCALATE,
        repair_attempts=3,
        detected_at=DETECTED_AT,
        evidence={"events": [{"event": "real_healing_history"}]},
    )
    db_session.add(real_incident)
    db_session.commit()
    before = {
        "status": real_incident.status,
        "classification": real_incident.classification,
        "recommended_action": real_incident.recommended_action,
        "repair_attempts": real_incident.repair_attempts,
        "evidence": real_incident.evidence,
        "recovery_proof": real_incident.recovery_proof,
    }

    first = start_demo(db_session)
    first_recovered = _advance(db_session, 7)
    second = start_demo(db_session)
    second_recovered = _advance(db_session, 7)

    assert first.external_collector_id == DEMO_EXTERNAL_COLLECTOR_ID
    assert first.provider == DEMO_PROVIDER
    assert first.collector_id != real_collector.id
    assert first_recovered.status == "self_healed"
    assert second.session_id != first.session_id
    assert second_recovered.status == "self_healed"
    demo_collector = db_session.get(Collector, second.collector_id)
    assert demo_collector is not None
    assert demo_collector.status is CollectorStatus.DISABLED
    assert demo_collector.source.active is False

    db_session.expire_all()
    unchanged = db_session.get(ReliabilityIncident, real_incident.id)
    assert unchanged is not None
    assert {
        "status": unchanged.status,
        "classification": unchanged.classification,
        "recommended_action": unchanged.recommended_action,
        "repair_attempts": unchanged.repair_attempts,
        "evidence": unchanged.evidence,
        "recovery_proof": unchanged.recovery_proof,
    } == before


def test_non_demo_incident_in_the_fixture_namespace_is_still_refused(
    db_session: Session,
    source: Source,
) -> None:
    contaminated = Collector(
        source_id=source.id,
        provider=DEMO_PROVIDER,
        external_collector_id=DEMO_EXTERNAL_COLLECTOR_ID,
        name="contaminated fixture collector",
        status=CollectorStatus.DISABLED,
    )
    db_session.add(contaminated)
    db_session.flush()
    incident = ReliabilityIncident(
        collector_id=contaminated.id,
        status=IncidentStatus.DEGRADED,
        classification=FailureClassification.EXTRACTION_DRIFT,
        recommended_action=RecommendedAction.REQUEST_HEAL,
        repair_attempts=0,
        detected_at=DETECTED_AT,
        evidence={"events": [{"event": "not_demo_owned"}]},
    )
    db_session.add(incident)
    db_session.commit()

    with pytest.raises(DemoIsolationError, match="non-demo incident"):
        start_demo(db_session)

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.evidence == {"events": [{"event": "not_demo_owned"}]}
