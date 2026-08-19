"""Incident listing and detail, including the derived timeline."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident, Source
from app.domain.enums import (
    CollectorStatus,
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    RunStatus,
)
from app.recallguard.demo import DEMO_EXTERNAL_COLLECTOR_ID, DEMO_PROVIDER
from app.recallguard.live_evidence import LIVE_HEALING_EXTERNAL_COLLECTOR_ID
from tests.opportunity_engine.conftest import OBSERVED_AT, make_signal
from tests.opportunity_engine.test_service import open_incident


def test_reliability_overview_lists_every_collector_with_its_state(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
    run: CollectorRun,
) -> None:
    body = api_client.get("/api/v1/reliability").json()

    assert body["state"] == "healthy"
    assert len(body["collectors"]) == 1
    view = body["collectors"][0]
    assert view["collector_id"] == str(collector.id)
    assert view["state"] == "healthy"
    assert view["active_incident"] is None
    assert view["last_run_id"] == str(run.id)
    assert view["last_record_count"] == run.record_count


def test_a_healthy_collector_has_no_incident_row_at_all(
    api_client: TestClient, collector: Collector
) -> None:
    assert api_client.get("/api/v1/reliability/incidents").json() == []


def test_incidents_are_listed_and_filterable(
    api_client: TestClient, db_session: Session, collector: Collector
) -> None:
    incident = open_incident(db_session, collector)

    listed = api_client.get("/api/v1/reliability/incidents").json()
    assert [item["id"] for item in listed] == [str(incident.id)]
    assert listed[0]["status"] == "degraded"
    assert listed[0]["recommended_action"] == "request_heal"
    assert listed[0]["repair_attempts"] == 0

    by_collector = api_client.get(
        "/api/v1/reliability/incidents", params={"collector_id": str(collector.id)}
    ).json()
    assert len(by_collector) == 1

    by_status = api_client.get(
        "/api/v1/reliability/incidents", params={"status": "manual_review"}
    ).json()
    assert by_status == []


def test_incident_detail_exposes_evidence_proof_and_a_derived_timeline(
    api_client: TestClient,
    db_session: Session,
    collector: Collector,
    run: CollectorRun,
) -> None:
    incident = open_incident(db_session, collector)
    incident.detection_run_id = run.id
    incident.evidence = {
        "occurrences": [
            {
                "detected_at": OBSERVED_AT.isoformat(),
                "collector_run_id": str(run.id),
                "recommended_action": "request_heal",
            }
        ],
        "events": [
            {
                "event": "healing_started",
                "at": "2026-08-18T12:05:00+00:00",
                "attempt": 1,
            },
            {
                "event": "repair_candidate_registered",
                "at": "2026-08-18T12:06:00+00:00",
                "attempt": 1,
            },
            {
                "event": "verification_passed",
                "at": "2026-08-18T12:10:00+00:00",
                "attempt": 1,
                "collector_run_id": str(run.id),
            },
        ],
    }
    incident.status = IncidentStatus.RECOVERED
    incident.verification_run_id = run.id
    incident.recovery_proof = {"result": "pass", "checks": []}
    db_session.commit()

    body = api_client.get(f"/api/v1/reliability/incidents/{incident.id}").json()

    assert body["status"] == "recovered"
    assert body["detection_run_id"] == str(run.id)
    assert body["verification_run_id"] == str(run.id)
    assert body["recovery_proof"] == {"result": "pass", "checks": []}
    assert body["evidence"]["events"][0]["event"] == "healing_started"

    timeline = [entry["event"] for entry in body["timeline"]]
    assert timeline == [
        "detected",
        "degradation_observed",
        "healing_started",
        "repair_candidate_registered",
        "verification_passed",
    ]
    assert body["timeline"][2]["attempt"] == 1


def test_a_timeline_is_never_invented_where_evidence_is_missing(
    api_client: TestClient, db_session: Session, collector: Collector
) -> None:
    incident = open_incident(db_session, collector)
    incident.evidence = None
    db_session.commit()

    body = api_client.get(f"/api/v1/reliability/incidents/{incident.id}").json()

    assert [entry["event"] for entry in body["timeline"]] == ["detected"]


def test_an_unknown_incident_is_a_404(api_client: TestClient) -> None:
    response = api_client.get(f"/api/v1/reliability/incidents/{uuid.uuid4()}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_live_evidence_is_unavailable_without_the_isolated_real_collector(
    api_client: TestClient,
) -> None:
    response = api_client.get("/api/v1/reliability/live-evidence")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["mode"] == "persisted_real_brightdata_run"
    assert body["collector"] is None
    assert body["live_trigger_safe"] is False


def test_live_evidence_exposes_real_runs_without_mutating_history(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
) -> None:
    real_collector = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id=LIVE_HEALING_EXTERNAL_COLLECTOR_ID,
        name="gapradar-fix-my-itch-heal-demo",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(real_collector)
    db_session.flush()
    broken_run = CollectorRun(
        collector_id=real_collector.id,
        external_run_id="j_broken_real_provider_job",
        status=RunStatus.FAILED,
        started_at=OBSERVED_AT,
        completed_at=OBSERVED_AT,
        record_count=0,
        raw_metadata={
            "orchestration": {
                "stage": "source_validation",
                "fetched_record_count": 133,
                "valid_record_count": 0,
                "invalid_record_count": 133,
            }
        },
    )
    verification_run = CollectorRun(
        collector_id=real_collector.id,
        external_run_id="j_fresh_real_provider_job",
        status=RunStatus.SUCCEEDED,
        started_at=OBSERVED_AT,
        completed_at=OBSERVED_AT,
        record_count=10,
        raw_metadata={
            "orchestration": {
                "stage": "completed",
                "fetched_record_count": 10,
                "valid_record_count": 10,
                "invalid_record_count": 0,
            }
        },
    )
    db_session.add_all([broken_run, verification_run])
    db_session.flush()
    make_signal(
        db_session,
        source,
        verification_run,
        title="A corrected real provider record",
        tam_score=7,
    )
    evidence = {
        "occurrences": [
            {
                "classification": "extraction_drift",
                "recommended_action": "request_heal",
                "observed_record_count": 133,
                "sample_violations": [
                    {
                        "index": 0,
                        "reason": "invalid_score",
                        "detail": "tam_score: Input should be less than or equal to 10",
                        "raw": {"problem": "Broken real record", "tam_score": 70},
                    }
                ],
            }
        ],
        "events": [
            {
                "event": "repair_candidate_registered",
                "attempt": 2,
                "candidate": {
                    "provider_status": "awaiting_approval",
                    "has_diff": True,
                    "preview_records": 2,
                },
            },
            {
                "event": "candidate_approved",
                "attempt": 2,
                "provider_status": "done",
                "note": "repair deployed; recovery still requires a fresh run",
                "preflight": {
                    "preview_records": 2,
                    "valid_records": 2,
                    "invalid_records": 0,
                },
            },
            {
                "event": "verification_failed",
                "attempt": 2,
                "collector_run_id": str(verification_run.id),
                "failed_checks": [
                    {
                        "name": "completeness",
                        "expected": "at most a 50% drop from baseline",
                        "observed": "10 records (92% drop)",
                        "passed": False,
                    }
                ],
            },
        ],
    }
    incident = ReliabilityIncident(
        collector_id=real_collector.id,
        detection_run_id=broken_run.id,
        status=IncidentStatus.MANUAL_REVIEW,
        classification=FailureClassification.EXTRACTION_DRIFT,
        recommended_action=RecommendedAction.ESCALATE,
        repair_attempts=3,
        detected_at=OBSERVED_AT,
        evidence=evidence,
        recovery_proof=None,
    )
    db_session.add(incident)
    db_session.commit()
    historical_evidence = incident.evidence
    product_before = (
        collector.name,
        collector.status,
        collector.external_collector_id,
    )

    response = api_client.get("/api/v1/reliability/live-evidence")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["mode"] == "persisted_real_brightdata_run"
    assert body["live_trigger_safe"] is False
    assert body["collector"]["collector_id"] == str(real_collector.id)
    assert body["collector"]["external_collector_id"] == LIVE_HEALING_EXTERNAL_COLLECTOR_ID
    assert body["broken_run"] == {
        "collector_run_id": str(broken_run.id),
        "provider_job_id": "j_broken_real_provider_job",
        "status": "failed",
        "started_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        "completed_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        "fetched_record_count": 133,
        "valid_record_count": 0,
        "invalid_record_count": 133,
        "accepted_record_count": 0,
    }
    assert body["invalid_records"][0]["value"] == 70
    assert body["invalid_records"][0]["allowed_max"] == 10
    assert body["invalid_records"][0]["reason"] == "invalid_score"
    assert body["detection"]["classification"] == "extraction_drift"
    assert body["detection"]["recommended_action"] == "request_heal"
    assert body["detection"]["severity"] is None
    assert body["detection"]["confidence"] is None
    assert body["repair_patch_available"] is False
    assert body["repair_attempts"][0]["status"] == "verification_rejected"
    assert body["repair_attempts"][0]["deployed"] is True
    assert body["verification"]["run"]["provider_job_id"] == "j_fresh_real_provider_job"
    assert body["verification"]["samples"][0]["tam_score"] == 7
    assert body["verification"]["contract_validation"] == "PASS"
    assert body["verification"]["regression_result"] == "FAIL"
    assert body["verification"]["final_decision"] == "REJECT"
    assert body["verification"]["final_status"] == "manual_review"
    assert body["verification"]["recovery_proof"] is None

    db_session.expire_all()
    assert db_session.get(ReliabilityIncident, incident.id).evidence == historical_evidence
    product = db_session.get(Collector, collector.id)
    assert product is not None
    assert (product.name, product.status, product.external_collector_id) == product_before


def test_demo_contract_starts_healthy_and_advances_one_persisted_step(
    api_client: TestClient,
) -> None:
    initial = api_client.get("/api/v1/reliability/demo")
    assert initial.status_code == 200
    assert initial.json()["status"] == "healthy"
    assert initial.json()["session_id"] is None

    started = api_client.post("/api/v1/reliability/demo/start")
    assert started.status_code == 200
    assert started.json()["mode"] == "fixture_replay"
    assert started.json()["status"] == "healthy"
    assert started.json()["session_id"] is not None

    degraded = api_client.post("/api/v1/reliability/demo/advance")
    assert degraded.status_code == 200
    body = degraded.json()
    assert body["status"] == "drift_detected"
    assert body["classification"] == "extraction_drift"
    assert body["recommended_action"] == "request_heal"
    assert {row["field"] for row in body["field_health"]} == {
        "title",
        "hazard",
        "remedy",
        "units",
    }


def test_demo_advance_requires_an_explicit_start(api_client: TestClient) -> None:
    response = api_client.post("/api/v1/reliability/demo/advance")

    assert response.status_code == 409
    assert "start" in response.json()["detail"]


def test_demo_endpoint_cannot_mutate_the_product_collector(
    api_client: TestClient,
    db_session: Session,
    collector: Collector,
) -> None:
    before = {
        "source_id": collector.source_id,
        "provider": collector.provider,
        "external_collector_id": collector.external_collector_id,
        "name": collector.name,
        "status": collector.status,
    }

    api_client.post("/api/v1/reliability/demo/start")
    for _ in range(7):
        response = api_client.post("/api/v1/reliability/demo/advance")
        assert response.status_code == 200

    db_session.expire_all()
    product = db_session.get(Collector, collector.id)
    assert product is not None
    assert {
        "source_id": product.source_id,
        "provider": product.provider,
        "external_collector_id": product.external_collector_id,
        "name": product.name,
        "status": product.status,
    } == before
    product_runs = db_session.execute(
        select(func.count())
        .select_from(CollectorRun)
        .where(CollectorRun.collector_id == collector.id)
    ).scalar_one()
    product_incidents = db_session.execute(
        select(func.count())
        .select_from(ReliabilityIncident)
        .where(ReliabilityIncident.collector_id == collector.id)
    ).scalar_one()
    assert product_runs == 0
    assert product_incidents == 0


def test_demo_api_does_not_reuse_the_real_healing_collector(
    api_client: TestClient,
    db_session: Session,
    source: Source,
) -> None:
    real_collector = Collector(
        source_id=source.id,
        provider="brightdata",
        external_collector_id="c_msya3ha629w2q9c62m",
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
        detected_at=OBSERVED_AT,
        evidence={"events": [{"event": "real_history"}]},
    )
    db_session.add(real_incident)
    db_session.commit()

    before_start = api_client.get("/api/v1/reliability/demo")
    assert before_start.status_code == 200
    initial = before_start.json()
    assert initial["provider"] == DEMO_PROVIDER
    assert initial["external_collector_id"] == DEMO_EXTERNAL_COLLECTOR_ID
    assert initial["collector_id"] is None

    response = api_client.post("/api/v1/reliability/demo/start")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == DEMO_PROVIDER
    assert body["external_collector_id"] == DEMO_EXTERNAL_COLLECTOR_ID
    assert body["collector_id"] != str(real_collector.id)
    db_session.refresh(real_incident)
    assert real_incident.status is IncidentStatus.MANUAL_REVIEW
    assert real_incident.recommended_action is RecommendedAction.ESCALATE
    assert real_incident.evidence == {"events": [{"event": "real_history"}]}
