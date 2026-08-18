"""The pipeline surface: claim a refresh, then poll for the answer.

Every provider call in these tests goes through an httpx.MockTransport.
The frontend posts a collector id; this process holds the credentials and
RecallGuard decides what the answer means.

What these tests are really pinning down is that the POST is a CLAIM, not
the work. The request must not scrape, must not wait, and must not reach
Bright Data at all -- so most of them use the client whose provider
handler fails the test if it is ever called.
"""

import uuid
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Collector, PipelineRun
from app.domain.enums import PipelineRunStatus
from app.integrations.brightdata.client import BrightDataClient
from app.pipeline.executor import drive_pipeline_run
from tests.api.conftest import RecordingScheduler
from tests.pipeline.conftest import RepairableProvider
from tests.recallguard.healing_fakes import ScriptedProvider, awaiting_approval, done


def records(count: int, production: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in production[:count]]


# -- POST /pipeline/run: claim only ----------------------------------------


def test_a_refresh_is_accepted_without_touching_the_provider(
    api_client: TestClient,
    collector: Collector,
    scheduler: RecordingScheduler,
) -> None:
    """202 and an execution id, with no Bright Data call inside the request.

    api_client's provider handler raises on any request, so a synchronous
    collection here would fail the test rather than merely be slow. That
    is the whole point of the change: the HTTP request has no
    relationship to how long the scrape takes.
    """
    response = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    )

    assert response.status_code == 202
    body = response.json()
    assert uuid.UUID(body["pipeline_run_id"])
    assert body["collector_id"] == str(collector.id)
    assert body["status"] == "queued"
    assert body["already_running"] is False
    # Claimed, and handed to the executor rather than executed inline.
    assert scheduler.scheduled == [uuid.UUID(body["pipeline_run_id"])]


def test_the_claim_is_persisted_before_the_response(
    api_client: TestClient, db_session: Session, collector: Collector
) -> None:
    body = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    ).json()

    run = db_session.get(PipelineRun, uuid.UUID(body["pipeline_run_id"]))
    assert run is not None
    assert run.status is PipelineRunStatus.QUEUED
    assert run.collector_id == collector.id
    # Nothing has been collected, so nothing has been decided.
    assert run.provider_job_id is None
    assert run.trusted is None
    assert run.completed_at is None


def test_a_second_request_joins_the_active_execution(
    api_client: TestClient,
    collector: Collector,
    scheduler: RecordingScheduler,
) -> None:
    """Asking twice must not scrape twice."""
    first = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    ).json()
    second = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    )

    assert second.status_code == 202
    body = second.json()
    assert body["pipeline_run_id"] == first["pipeline_run_id"]
    assert body["already_running"] is True
    # One claim, one scheduled execution -- the second request scheduled
    # nothing, so no second Bright Data job can be triggered.
    assert scheduler.scheduled == [uuid.UUID(first["pipeline_run_id"])]


def test_an_unknown_collector_is_a_404_and_never_reaches_the_provider(
    api_client: TestClient, scheduler: RecordingScheduler
) -> None:
    response = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404
    assert scheduler.scheduled == []


def test_a_malformed_request_is_rejected(api_client: TestClient) -> None:
    assert (
        api_client.post(
            "/api/v1/pipeline/run", json={"collector_id": "nope"}
        ).status_code
        == 422
    )


# -- GET /pipeline/runs/{id} -----------------------------------------------


def test_an_unknown_pipeline_run_is_a_404(api_client: TestClient) -> None:
    response = api_client.get(f"/api/v1/pipeline/runs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_a_claimed_execution_reports_no_verdict_yet(
    api_client: TestClient, collector: Collector
) -> None:
    """An in-flight refresh has reached no conclusion about the data.

    `trusted` must be null rather than false: "not decided yet" and
    "judged untrustworthy" are different facts, and reporting the second
    would show a degradation that has not happened.
    """
    claimed = api_client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    ).json()

    body = api_client.get(f"/api/v1/pipeline/runs/{claimed['pipeline_run_id']}").json()

    assert body["pipeline_run_id"] == claimed["pipeline_run_id"]
    assert body["collector_id"] == str(collector.id)
    assert body["status"] == "queued"
    assert body["trusted"] is None
    assert body["reliability_state"] is None
    assert body["incident_id"] is None
    assert body["provider_job_id"] is None
    assert body["completed_at"] is None
    assert body["error"] is None
    assert body["updated_at"] is not None


# -- the executed cycle, seen through the status endpoint ------------------


def drive(
    client: TestClient,
    db_session: Session,
    provider: ScriptedProvider,
    brightdata: BrightDataClient,
    collector: Collector,
) -> dict[str, Any]:
    """Claim a refresh through the API, then run the claimed work.

    Stands in for the background executor, using the test's own session
    and mock transport rather than the real ones the local executor would
    open for itself.
    """
    claimed = client.post(
        "/api/v1/pipeline/run", json={"collector_id": str(collector.id)}
    ).json()
    drive_pipeline_run(
        db_session,
        brightdata,
        pipeline_run_id=uuid.UUID(claimed["pipeline_run_id"]),
        sleep=lambda _seconds: None,
    )
    return client.get(f"/api/v1/pipeline/runs/{claimed['pipeline_run_id']}").json()


def test_a_completed_execution_exposes_its_trust_verdict(
    make_api_client: Callable[..., TestClient],
    db_session: Session,
    collector: Collector,
    production_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[done()], dataset=records(5, production_records)
    )
    client = make_api_client(provider)

    with _provider_client(provider) as brightdata:
        body = drive(client, db_session, provider, brightdata, collector)

    assert body["status"] == "completed"
    assert body["trusted"] is True
    assert body["reliability_state"] == "healthy"
    assert body["incident_id"] is None
    assert body["provider_job_id"] is not None
    assert body["collector_run_id"] is not None
    assert body["completed_at"] is not None
    assert body["error"] is None

    # The refreshed data is the data the product serves.
    assert len(client.get("/api/v1/opportunities").json()) == 5


def test_drift_completes_the_execution_as_degraded_not_failed(
    make_api_client: Callable[..., TestClient],
    db_session: Session,
    collector: Collector,
    production_records: list[dict[str, Any]],
) -> None:
    """The deliberate scraper fault, seen through the async surface.

    DEGRADED, not FAILED: the execution ran to completion and RecallGuard
    judged the result untrustworthy, which is the system working. FAILED
    is reserved for an execution that could not be carried out at all.
    """
    drifted = records(5, production_records)
    drifted[0] = {**drifted[0], "tam_score": 60}
    provider = ScriptedProvider(progress=[awaiting_approval(drifted)], dataset=drifted)
    client = make_api_client(provider)

    with _provider_client(provider) as brightdata:
        body = drive(client, db_session, provider, brightdata, collector)

    assert body["status"] == "degraded"
    assert body["trusted"] is False
    assert body["reliability_state"] == "degraded"
    assert body["incident_id"] is not None
    assert body["error"] is None

    incidents = client.get("/api/v1/reliability/incidents").json()
    assert [item["id"] for item in incidents] == [body["incident_id"]]
    assert incidents[0]["classification"] == "extraction_drift"

    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["pipeline"]["state"] == "degraded"
    assert dashboard["top_opportunities"] == []


def test_a_proven_repair_completes_the_execution_as_trusted(
    make_api_client: Callable[..., TestClient],
    db_session: Session,
    collector: Collector,
    production_records: list[dict[str, Any]],
) -> None:
    good = records(5, production_records)
    drifted = [{**good[0], "tam_score": 60}, *good[1:]]
    provider = RepairableProvider(
        broken=drifted, healed=good, progress=[awaiting_approval(good), done()]
    )
    client = make_api_client(provider)

    with _provider_client(provider) as brightdata:
        body = drive(client, db_session, provider, brightdata, collector)

    assert body["status"] == "completed"
    assert body["trusted"] is True

    detail = client.get(f"/api/v1/reliability/incidents/{body['incident_id']}").json()
    assert detail["status"] == "recovered"
    assert detail["recovery_proof"]["result"] == "pass"
    # The execution stands behind the independent verification run, not
    # the collection that detected the fault.
    assert detail["verification_run_id"] == body["collector_run_id"]
    assert detail["verification_run_id"] != detail["detection_run_id"]

    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["pipeline"]["state"] == "healthy"
    assert len(dashboard["top_opportunities"]) == 5


def _provider_client(provider: ScriptedProvider) -> BrightDataClient:
    """A BrightDataClient bound to the scripted transport."""
    return BrightDataClient(
        settings=Settings(
            _env_file=None,
            BRIGHTDATA_API_KEY="test-token-do-not-log",
            BRIGHTDATA_BASE_URL="https://api.brightdata.test",
        ),
        transport=httpx.MockTransport(provider),
    )
