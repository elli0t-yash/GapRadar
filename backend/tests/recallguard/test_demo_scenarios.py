"""The narrative RecallGuard exists to prove:

    a repair was proposed, and an independent fresh collection decided
    whether it worked.

Also guards the scope of this phase: RecallGuard tracks the healing
lifecycle without ever asking a provider to heal anything.
"""

from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Collector, ReliabilityIncident
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
)
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

BASELINE = BaselineProfile(label="fix_my_itch_healthy_v1", record_count=133)


def test_semantic_drift_is_detected_healed_and_proven_by_a_fresh_run(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    clock = FakeClock()

    # A. healthy production-style collection.
    healthy = evaluate_collector_run(
        db_session, run=runs.succeeded(record_count=133), baseline=BASELINE, now=clock
    )
    assert healthy.passed is True
    assert healthy.state is ReliabilityState.HEALTHY

    # B. semantic drift: the scraper fills every field, with a TAM score
    # on the wrong scale.
    drift = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=133
        ),
        baseline=BASELINE,
        now=clock,
    )
    assert drift.classification is FailureClassification.EXTRACTION_DRIFT
    assert drift.recommended_action is RecommendedAction.REQUEST_HEAL
    incident = db_session.get(ReliabilityIncident, drift.incident_id)
    assert incident is not None

    # A repair is proposed. It proves nothing yet.
    start_healing(db_session, incident, now=clock)
    register_repair_candidate(
        db_session, incident, candidate={"note": "selector rewritten"}, now=clock
    )
    assert incident.status is IncidentStatus.VALIDATING
    assert incident.recovery_proof is None

    # C. the first verification run still collapses to zero records:
    # the repair did not work, and RecallGuard says so.
    failed_verification = verify_recovery(
        db_session,
        incident,
        verification_run=runs.succeeded(
            record_count=0, started_at=DETECTED_AT + timedelta(hours=1)
        ),
        baseline=BASELINE,
        now=clock,
    )
    assert failed_verification.passed is False
    assert incident.status is IncidentStatus.DEGRADED

    # Second attempt, and this time a fresh independent collection comes
    # back clean.
    start_healing(db_session, incident, now=clock)
    register_repair_candidate(db_session, incident, now=clock)
    verification_run = runs.succeeded(
        record_count=131, started_at=DETECTED_AT + timedelta(hours=2)
    )
    recovered = verify_recovery(
        db_session,
        incident,
        verification_run=verification_run,
        baseline=BASELINE,
        now=clock,
    )

    assert recovered.passed is True
    assert incident.status is IncidentStatus.RECOVERED
    proof = incident.recovery_proof
    assert proof["detection_run_id"] == str(incident.detection_run_id)
    assert proof["verification_run_id"] == str(verification_run.id)
    assert proof["repair_attempt"] == 2
    assert proof["result"] == "pass"
    # The incident is closed, so the collector is healthy again -- while
    # the proven repair stays on the record.
    assert (
        collector_reliability_state(db_session, collector_id=collector.id)
        is ReliabilityState.HEALTHY
    )


def test_recallguard_never_invokes_provider_healing() -> None:
    # Task 4 wires Bright Data's heal/approve/reject in. This phase must
    # only decide *whether* a repair is warranted and *whether* it worked.
    package = Path(__file__).resolve().parents[2] / "app" / "recallguard"
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in package.glob("*.py")
    )

    for forbidden in (
        "request_healing",
        "approve_healing",
        "reject_healing",
        "BrightDataClient",
        "refactor_template",
        "resume_automation_job",
        "httpx",
    ):
        assert forbidden not in sources
