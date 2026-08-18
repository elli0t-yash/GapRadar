"""Collectors and their execution history."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, Source
from app.domain.enums import RunStatus
from tests.opportunity_engine.conftest import make_run


def test_collectors_are_listed(
    api_client: TestClient, collector: Collector, source: Source
) -> None:
    body = api_client.get("/api/v1/collectors").json()

    assert [item["id"] for item in body] == [str(collector.id)]
    assert body[0]["external_collector_id"] == collector.external_collector_id
    assert body[0]["status"] == "active"


def test_collector_runs_are_returned_newest_first(
    api_client: TestClient,
    db_session: Session,
    collector: Collector,
    run: CollectorRun,
) -> None:
    later = make_run(
        db_session,
        collector,
        external_run_id="j_later",
        status=RunStatus.FAILED,
        record_count=0,
    )
    later.started_at = run.started_at.replace(hour=13)
    db_session.commit()

    body = api_client.get(f"/api/v1/collectors/{collector.id}/runs").json()

    assert [item["id"] for item in body] == [str(later.id), str(run.id)]
    assert body[0]["status"] == "failed"
    assert body[1]["record_count"] == run.record_count
    # The run carries its orchestration evidence, not a trust verdict.
    assert body[1]["raw_metadata"]["orchestration"]["stage"] == "completed"


def test_the_run_limit_is_respected(
    api_client: TestClient, collector: Collector, run: CollectorRun
) -> None:
    body = api_client.get(
        f"/api/v1/collectors/{collector.id}/runs", params={"limit": 1}
    ).json()

    assert len(body) == 1


def test_runs_for_an_unknown_collector_are_a_404(api_client: TestClient) -> None:
    response = api_client.get(f"/api/v1/collectors/{uuid.uuid4()}/runs")

    assert response.status_code == 404
