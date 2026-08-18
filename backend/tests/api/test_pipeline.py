"""POST /pipeline/run: the only write surface, and the only path to Bright Data.

Every provider call in these tests goes through an httpx.MockTransport.
The frontend posts a collector id; this process holds the credentials and
RecallGuard decides what the answer means.
"""

import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collector, ReliabilityIncident, Source
from app.domain.enums import IncidentStatus
from tests.pipeline.conftest import RepairableProvider
from tests.recallguard.healing_fakes import ScriptedProvider, awaiting_approval, done


def records(count: int, production: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in production[:count]]


def test_a_healthy_run_is_reported_as_trusted(
    make_api_client: Callable[..., TestClient],
    collector: Collector,
    production_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[done()], dataset=records(5, production_records)
    )
    client = make_api_client(provider)

    response = client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "healthy"
    assert body["reliability_state"] == "healthy"
    assert body["trusted"] is True
    assert body["trusted_collector_run_id"] == body["collector_run_id"]
    assert body["incident_id"] is None
    assert body["collection"]["accepted"] == 5


def test_drift_opens_an_incident_and_leaves_the_data_untrusted(
    make_api_client: Callable[..., TestClient],
    db_session: Session,
    collector: Collector,
    production_records: list[dict[str, Any]],
) -> None:
    """The deliberate scraper fault, seen from the demo's own endpoints."""
    drifted = records(5, production_records)
    drifted[0] = {**drifted[0], "tam_score": 60}
    # The candidate's preview still carries the fault, so it is rejected.
    provider = ScriptedProvider(progress=[awaiting_approval(drifted)], dataset=drifted)
    client = make_api_client(provider)

    body = client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    ).json()

    assert body["trusted"] is False
    assert body["trusted_collector_run_id"] is None
    assert body["evaluation"]["classification"] == "extraction_drift"
    assert body["evaluation"]["recommended_action"] == "request_heal"

    incidents = client.get("/api/v1/reliability/incidents").json()
    assert [item["id"] for item in incidents] == [body["incident_id"]]
    assert incidents[0]["status"] == "degraded"

    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["pipeline"]["state"] == "degraded"
    assert dashboard["top_opportunities"] == []
    assert dashboard["signals"]["trusted"] == 0


def test_a_proven_repair_recovers_the_incident_and_restores_the_data(
    make_api_client: Callable[..., TestClient],
    db_session: Session,
    source: Source,
    collector: Collector,
    production_records: list[dict[str, Any]],
) -> None:
    good = records(5, production_records)
    drifted = [{**good[0], "tam_score": 60}, *good[1:]]
    provider = RepairableProvider(
        broken=drifted, healed=good, progress=[awaiting_approval(good), done()]
    )
    client = make_api_client(provider)

    body = client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    ).json()

    assert body["outcome"] == "recovered"
    assert body["trusted"] is True

    incident = db_session.get(ReliabilityIncident, uuid.UUID(body["incident_id"]))
    assert incident is not None
    assert incident.status is IncidentStatus.RECOVERED

    detail = client.get(f"/api/v1/reliability/incidents/{incident.id}").json()
    assert detail["recovery_proof"]["result"] == "pass"
    assert detail["verification_run_id"] == body["trusted_collector_run_id"]
    assert detail["verification_run_id"] != detail["detection_run_id"]

    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["pipeline"]["state"] == "healthy"
    assert dashboard["recallguard"]["recovered_incident_count"] == 1
    assert len(dashboard["top_opportunities"]) == 5


def test_an_unknown_collector_is_a_404_and_never_reaches_the_provider(
    api_client: TestClient,
) -> None:
    """api_client's provider handler fails the test if it is ever called."""
    response = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404


def test_a_malformed_request_is_rejected(api_client: TestClient) -> None:
    assert (
        api_client.post(
            "/api/v1/pipeline/run", json={"collector_id": "nope"}
        ).status_code
        == 422
    )
