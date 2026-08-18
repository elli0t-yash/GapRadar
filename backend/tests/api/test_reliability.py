"""Incident listing and detail, including the derived timeline."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, Source
from app.domain.enums import IncidentStatus
from tests.opportunity_engine.conftest import OBSERVED_AT
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
