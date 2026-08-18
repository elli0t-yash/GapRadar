"""The opportunity surface exposes trusted signals and nothing else."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, Source
from tests.opportunity_engine.conftest import (
    make_collector,
    make_run,
    make_signal,
)
from tests.opportunity_engine.test_service import open_incident


def test_opportunities_are_ranked_and_carry_their_component_scores(
    api_client: TestClient, db_session: Session, source: Source, run: CollectorRun
) -> None:
    make_signal(db_session, source, run, title="low", itch_score=10)
    make_signal(db_session, source, run, title="high", itch_score=100)

    body = api_client.get("/api/v1/opportunities").json()

    assert [item["title"] for item in body] == ["high", "low"]
    top = body[0]
    assert top["problem"] == "high"
    assert top["industry"] == "B2B Services"
    assert top["tam_score"] == 7
    assert top["source"] == "fix_my_itch"
    assert top["source_url"] == "https://razorpay.com/m/fix-my-itch/"
    assert top["opportunity_score"] == 30.0 + 16.0 + 14.0 + 12.0 + 5.0


def test_only_trusted_signals_are_exposed(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    collector: Collector,
    run: CollectorRun,
) -> None:
    untrusted = make_signal(db_session, source, run, title="untrusted")
    open_incident(db_session, collector)

    healthy = make_collector(
        db_session, source, name="second", external_collector_id="c_second"
    )
    trusted = make_signal(
        db_session,
        source,
        make_run(db_session, healthy, external_run_id="j_second"),
        title="trusted",
    )

    body = api_client.get("/api/v1/opportunities").json()

    assert [item["id"] for item in body] == [str(trusted.id)]
    assert api_client.get(f"/api/v1/opportunities/{untrusted.id}").status_code == 404
    assert api_client.get(f"/api/v1/opportunities/{trusted.id}").status_code == 200


def test_an_unknown_opportunity_is_a_404(api_client: TestClient) -> None:
    assert api_client.get(f"/api/v1/opportunities/{uuid.uuid4()}").status_code == 404


def test_the_limit_is_respected(
    api_client: TestClient, db_session: Session, source: Source, run: CollectorRun
) -> None:
    for index in range(3):
        make_signal(db_session, source, run, title=f"p{index}", itch_score=index * 10)

    body = api_client.get("/api/v1/opportunities", params={"limit": 2}).json()

    assert len(body) == 2
