"""The one aggregated read the frontend lands on."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, Source
from app.domain.enums import IncidentStatus
from tests.opportunity_engine.conftest import make_signal
from tests.opportunity_engine.test_service import open_incident


def test_an_empty_database_yields_an_empty_dashboard(api_client: TestClient) -> None:
    """Zeros and nulls, never a plausible-looking sample."""
    response = api_client.get("/api/v1/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["pipeline"] == {
        "state": "healthy",
        "last_run_at": None,
        "last_run_id": None,
        "last_run_status": None,
        "last_record_count": None,
        "last_collector_id": None,
    }
    assert body["recallguard"]["state"] == "healthy"
    assert body["recallguard"]["active_incident"] is None
    assert body["recallguard"]["active_incident_count"] == 0
    assert body["signals"] == {"total": 0, "trusted": 0}
    assert body["top_opportunities"] == []


def test_a_healthy_pipeline_reports_its_last_run_and_its_opportunities(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
    run: CollectorRun,
) -> None:
    make_signal(db_session, source, run, title="high", itch_score=100)
    make_signal(db_session, source, run, title="low", itch_score=10)

    body = api_client.get("/api/v1/dashboard").json()

    assert body["pipeline"]["state"] == "healthy"
    assert body["pipeline"]["last_run_id"] == str(run.id)
    assert body["pipeline"]["last_run_status"] == "succeeded"
    assert body["pipeline"]["last_record_count"] == run.record_count
    assert body["pipeline"]["last_run_at"] is not None
    assert body["signals"] == {"total": 2, "trusted": 2}
    assert [item["title"] for item in body["top_opportunities"]] == ["high", "low"]
    assert body["top_opportunities"][0]["opportunity_score"] is not None


def test_an_active_incident_degrades_the_dashboard_and_withholds_the_data(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
    run: CollectorRun,
) -> None:
    """A run that finished is not a collector that can be trusted."""
    make_signal(db_session, source, run)
    incident = open_incident(db_session, collector)

    body = api_client.get("/api/v1/dashboard").json()

    assert body["pipeline"]["state"] == "degraded"
    assert body["recallguard"]["state"] == "degraded"
    assert body["recallguard"]["active_incident"]["id"] == str(incident.id)
    assert (
        body["recallguard"]["active_incident"]["classification"] == "extraction_drift"
    )
    assert body["recallguard"]["active_incident_count"] == 1
    assert body["signals"] == {"total": 1, "trusted": 0}
    assert body["top_opportunities"] == []


def test_the_headline_state_is_the_most_alarming_one(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
    run: CollectorRun,
) -> None:
    incident = open_incident(db_session, collector, status=IncidentStatus.MANUAL_REVIEW)

    body = api_client.get("/api/v1/dashboard").json()

    assert body["pipeline"]["state"] == "manual_review"
    assert body["recallguard"]["active_incident"]["id"] == str(incident.id)


def test_a_recovered_incident_leaves_the_dashboard_healthy_and_counted(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
    run: CollectorRun,
) -> None:
    """A proven repair is history, not a live problem -- and never erased."""
    incident = open_incident(db_session, collector)
    incident.status = IncidentStatus.RECOVERED
    db_session.commit()

    body = api_client.get("/api/v1/dashboard").json()

    assert body["pipeline"]["state"] == "healthy"
    assert body["recallguard"]["active_incident"] is None
    assert body["recallguard"]["active_incident_count"] == 0
    assert body["recallguard"]["recovered_incident_count"] == 1
