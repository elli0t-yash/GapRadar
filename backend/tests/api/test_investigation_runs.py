"""The investigation run and research API.

ZERO PROVIDER SPEND IS THE PROPERTY UNDER TEST. Every client here is
bound to a Bright Data transport that RAISES on contact, the OpenAI
client constructor is refused, and the background executor is a recorder
-- so a route that reached a provider fails loudly rather than passing
quietly on a mock that happened to answer.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Investigation, InvestigationRun, Signal
from app.domain.enums import InvestigationRunStatus
from app.investigations.runs import set_run_status
from tests.api.conftest import RecordingScheduler


@pytest.fixture
def refuse_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any OpenAI client construction fails the test."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected OpenAI client construction")

    monkeypatch.setattr("openai.OpenAI", refuse)


def create(client: TestClient, query: str = "Booking cargo vehicles is broken") -> str:
    response = client.post("/api/v1/investigations", json={"query": query})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# -- starting a run ---------------------------------------------------------


def test_starting_a_run_is_a_202_claim(
    api_client: TestClient, investigation_scheduler: RecordingScheduler
) -> None:
    """202, not 200: nothing has been searched when this returns."""
    investigation_id = create(api_client)

    response = api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    assert response.status_code == 202
    body = response.json()
    assert body["investigation_id"] == investigation_id
    assert body["status"] == "queued"
    assert body["already_running"] is False
    assert uuid.UUID(body["run_id"])
    assert investigation_scheduler.scheduled == [uuid.UUID(body["run_id"])]


def test_starting_a_run_spends_nothing_in_the_request(
    api_client: TestClient, refuse_openai: None
) -> None:
    """One lookup and one INSERT. The Bright Data transport refuses calls."""
    investigation_id = create(api_client)

    assert (
        api_client.post(
            f"/api/v1/investigations/{investigation_id}/run"
        ).status_code
        == 202
    )


def test_a_second_start_joins_the_run_already_in_flight(
    api_client: TestClient, investigation_scheduler: RecordingScheduler
) -> None:
    """A double-click must not buy a second set of searches."""
    investigation_id = create(api_client)

    first = api_client.post(f"/api/v1/investigations/{investigation_id}/run").json()
    second = api_client.post(f"/api/v1/investigations/{investigation_id}/run").json()

    assert second["run_id"] == first["run_id"]
    assert second["already_running"] is True
    assert len(investigation_scheduler.scheduled) == 1


def test_a_second_start_creates_no_second_run_row(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)

    api_client.post(f"/api/v1/investigations/{investigation_id}/run")
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    assert (
        db_session.execute(
            select(func.count()).select_from(InvestigationRun)
        ).scalar_one()
        == 1
    )


def test_starting_a_run_for_an_unknown_investigation_is_a_404(
    api_client: TestClient, investigation_scheduler: RecordingScheduler
) -> None:
    unknown = uuid.uuid4()

    response = api_client.post(f"/api/v1/investigations/{unknown}/run")

    assert response.status_code == 404
    assert str(unknown) in response.json()["detail"]
    assert investigation_scheduler.scheduled == []


def test_starting_a_run_creates_no_signal(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    assert (
        db_session.execute(select(func.count()).select_from(Signal)).scalar_one() == 0
    )


def test_a_stranded_run_does_not_block_a_new_one(
    api_client: TestClient, db_session: Session
) -> None:
    """What a Railway restart mid-investigation leaves behind.

    Without reconciliation the active-run index would make this
    investigation permanently un-runnable.
    """
    investigation_id = create(api_client)
    first = api_client.post(f"/api/v1/investigations/{investigation_id}/run").json()

    stranded = db_session.get(InvestigationRun, uuid.UUID(first["run_id"]))
    assert stranded is not None
    set_run_status(db_session, stranded, InvestigationRunStatus.RUNNING)
    stranded.created_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()

    second = api_client.post(f"/api/v1/investigations/{investigation_id}/run").json()

    assert second["already_running"] is False
    assert second["run_id"] != first["run_id"]


# -- reading a run ----------------------------------------------------------


def test_reading_a_run_before_anything_is_started_is_null(
    api_client: TestClient,
) -> None:
    """"Never asked" is a different fact from "asked and waiting"."""
    investigation_id = create(api_client)

    response = api_client.get(f"/api/v1/investigations/{investigation_id}/run")

    assert response.status_code == 200
    assert response.json() is None


def test_reading_a_run_reports_factual_progress(api_client: TestClient) -> None:
    """Zero of zero before a plan exists -- a fact, not a placeholder."""
    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    body = api_client.get(f"/api/v1/investigations/{investigation_id}/run").json()

    assert body["status"] == "queued"
    assert body["research_queries_total"] == 0
    assert body["research_queries_completed"] == 0
    assert body["counters"] == {
        "discovered": 0,
        "selected": 0,
        "judged": 0,
        "matched": 0,
    }
    assert body["is_terminal"] is False
    assert body["is_retryable"] is False


def test_the_run_read_carries_no_demand_or_competitor_counters(
    api_client: TestClient,
) -> None:
    """Phase 2 does no such work, so it reports no such number."""
    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    body = api_client.get(f"/api/v1/investigations/{investigation_id}/run").json()

    forbidden = {"demand", "competitors", "whitespace", "verdict"}
    assert not any(
        any(word in key for word in forbidden) for key in body
    ), f"unexpected phase-3 field in {sorted(body)}"


def test_reading_a_run_for_an_unknown_investigation_is_a_404(
    api_client: TestClient,
) -> None:
    assert (
        api_client.get(f"/api/v1/investigations/{uuid.uuid4()}/run").status_code == 404
    )


def test_reading_a_run_contacts_no_provider(
    api_client: TestClient,
    refuse_openai: None,
    investigation_scheduler: RecordingScheduler,
) -> None:
    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")
    investigation_scheduler.scheduled.clear()

    assert (
        api_client.get(f"/api/v1/investigations/{investigation_id}/run").status_code
        == 200
    )
    assert investigation_scheduler.scheduled == []


# -- reading the research ---------------------------------------------------


def test_research_for_a_never_run_investigation_is_empty_but_valid(
    api_client: TestClient,
) -> None:
    """An empty result, not a 404 and not a triggered run."""
    investigation_id = create(api_client)

    response = api_client.get(f"/api/v1/investigations/{investigation_id}/research")

    assert response.status_code == 200
    body = response.json()
    assert body["subject_id"] == investigation_id
    assert body["origin"] == "investigation"
    assert body["generated_queries"] == []
    assert body["paper_count"] == 0
    assert body["matched_paper_count"] == 0
    assert body["average_relevance_score"] is None
    assert body["top_papers"] == []


def test_reading_research_starts_nothing_and_spends_nothing(
    api_client: TestClient,
    refuse_openai: None,
    investigation_scheduler: RecordingScheduler,
    db_session: Session,
) -> None:
    """A GET that quietly triggered a provider run would make page loads
    cost money and make an idempotent-looking request mutate the database."""
    investigation_id = create(api_client)

    api_client.get(f"/api/v1/investigations/{investigation_id}/research")

    assert investigation_scheduler.scheduled == []
    assert (
        db_session.execute(
            select(func.count()).select_from(InvestigationRun)
        ).scalar_one()
        == 0
    )


def test_reading_research_for_an_unknown_investigation_is_a_404(
    api_client: TestClient,
) -> None:
    unknown = uuid.uuid4()
    response = api_client.get(f"/api/v1/investigations/{unknown}/research")

    assert response.status_code == 404
    assert str(unknown) in response.json()["detail"]


# Exactly the keys the investigation research endpoint serves. A literal,
# not a comparison against the opportunity endpoint: the two contracts are
# now deliberately DIFFERENT, and a test that only checked they matched
# would pass while both were wrong in the same direction -- which is
# precisely how `subject_id` and `origin` leaked onto a surface that
# predates them.
INVESTIGATION_RESEARCH_KEYS = {
    "subject_id",
    "origin",
    "generated_queries",
    "paper_count",
    "matched_paper_count",
    "average_relevance_score",
    "top_concepts",
    "top_papers",
}


def test_the_research_response_has_exactly_the_documented_keys(
    api_client: TestClient,
) -> None:
    investigation_id = create(api_client)

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/research"
    ).json()

    assert set(body) == INVESTIGATION_RESEARCH_KEYS


def test_the_research_response_carries_no_signal_id(
    api_client: TestClient,
) -> None:
    """No signal produced this, so the field is absent rather than null.

    A null `signal_id` would still invite a client to key off it; the
    subject-aware pair says what this actually is.
    """
    investigation_id = create(api_client)

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/research"
    ).json()

    assert "signal_id" not in body
    assert body["subject_id"] == investigation_id
    assert body["origin"] == "investigation"


def test_the_two_research_surfaces_are_deliberately_different(
    api_client: TestClient, db_session: Session
) -> None:
    """The shared payload is identical; only the subject naming differs.

    Asserted as a DIFFERENCE, not a match: the opportunity endpoint is
    frozen at the keys it shipped with, and the investigation endpoint is
    free to carry subject-aware ones.
    """
    from tests.research_intelligence.conftest import make_opportunity_signal

    signal = make_opportunity_signal(db_session, title="Cargo booking is broken")
    investigation_id = create(api_client)

    opportunity_body = api_client.get(
        f"/api/v1/opportunities/{signal.id}/research"
    ).json()
    investigation_body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/research"
    ).json()

    shared = {
        "generated_queries",
        "paper_count",
        "matched_paper_count",
        "average_relevance_score",
        "top_concepts",
        "top_papers",
    }
    assert set(opportunity_body) - shared == {"signal_id"}
    assert set(investigation_body) - shared == {"subject_id", "origin"}


def test_the_investigation_detail_reflects_a_claimed_run(
    api_client: TestClient,
) -> None:
    """READY, not RUNNING: work is claimed and nothing has started."""
    investigation_id = create(api_client)
    assert (
        api_client.get(f"/api/v1/investigations/{investigation_id}").json()["status"]
        == "draft"
    )

    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    assert (
        api_client.get(f"/api/v1/investigations/{investigation_id}").json()["status"]
        == "ready"
    )


def test_no_provider_call_is_made_by_any_read_on_this_surface(
    api_client: TestClient, refuse_openai: None, db_session: Session
) -> None:
    """One sweep over every GET, with both providers armed to fail."""
    investigation_id = create(api_client)

    for path in (
        "/api/v1/investigations",
        f"/api/v1/investigations/{investigation_id}",
        f"/api/v1/investigations/{investigation_id}/run",
        f"/api/v1/investigations/{investigation_id}/research",
    ):
        assert api_client.get(path).status_code == 200, path

    assert (
        db_session.execute(select(func.count()).select_from(Investigation)).scalar_one()
        == 1
    )
