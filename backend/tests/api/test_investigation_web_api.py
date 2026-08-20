"""The evidence and competitor read surfaces.

ZERO PROVIDER SPEND, and zero page fetches. Every client here is bound to
a Bright Data transport that raises on contact, OpenAI construction is
refused, and the background executor is a recorder. A GET that reached a
provider -- or opened a discovered URL -- would fail loudly.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import (
    InvestigationCompetitor,
    InvestigationDemandEvidence,
    InvestigationWebSearchHit,
    InvestigationWebSearchRun,
)
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    WebSearchStatus,
)
from tests.api.conftest import RecordingScheduler

DEMAND_KEYS = {"investigation_id", "counts", "evidence"}
DEMAND_ITEM_KEYS = {
    "id", "url", "domain", "title", "snippet", "published_at",
    "classification", "relevance_score", "reason", "provenance",
}
COMPETITOR_KEYS = {"investigation_id", "counts", "competitors"}
COMPETITOR_ITEM_KEYS = {
    "id", "url", "domain", "name", "snippet",
    "classification", "relevance_score", "reason", "provenance",
}


@pytest.fixture
def refuse_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected OpenAI client construction")

    monkeypatch.setattr("openai.OpenAI", refuse)


def create(client: TestClient) -> str:
    response = client.post(
        "/api/v1/investigations",
        json={"query": "Booking cargo vehicles is broken for small shippers"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def seed(
    session: Session,
    investigation_id: uuid.UUID,
    *,
    queries: tuple[str, ...] = ("cargo booking problems",),
    url: str = "https://a.test/1",
    family: str = "demand",
) -> None:
    """Persist searches, hits and one judged page, as a run would."""
    for query in queries:
        search_run = InvestigationWebSearchRun(
            investigation_id=investigation_id,
            family=family,
            query=query,
            provider="fake",
            product="fake_serp",
            locale_country="us",
            locale_language="en",
            status=WebSearchStatus.SUCCEEDED,
            records_returned=1,
            latency_ms=1200,
        )
        session.add(search_run)
        session.flush()
        session.add(
            InvestigationWebSearchHit(
                investigation_web_search_run_id=search_run.id,
                url=url,
                domain="a.test",
                title="Cargo booking is manual",
                snippet="Shippers struggle to book cargo vehicles.",
                position=2,
            )
        )
    if family == "demand":
        session.add(
            InvestigationDemandEvidence(
                investigation_id=investigation_id,
                url=url,
                domain="a.test",
                title="Cargo booking is manual",
                snippet="Shippers struggle to book cargo vehicles.",
                classification=DemandEvidenceClassification.SUPPORT,
                relevance_score=72.0,
                reason="Describes shippers experiencing this problem.",
            )
        )
    else:
        session.add(
            InvestigationCompetitor(
                investigation_id=investigation_id,
                url=url,
                domain="a.test",
                name="Cargo booking is manual",
                snippet="Shippers struggle to book cargo vehicles.",
                classification=CompetitorClassification.ADJACENT,
                relevance_score=64.0,
                reason="Neighbouring product for the same buyer.",
            )
        )
    session.commit()


# -- demand evidence --------------------------------------------------------


def test_evidence_for_a_never_run_investigation_is_empty_but_valid(
    api_client: TestClient,
) -> None:
    investigation_id = create(api_client)

    response = api_client.get(f"/api/v1/investigations/{investigation_id}/evidence")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DEMAND_KEYS
    assert body["investigation_id"] == investigation_id
    assert body["counts"] == {}
    assert body["evidence"] == []


def test_evidence_has_exactly_the_documented_keys(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id))

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/evidence"
    ).json()

    assert set(body) == DEMAND_KEYS
    assert set(body["evidence"][0]) == DEMAND_ITEM_KEYS


def test_evidence_carries_no_demand_score(
    api_client: TestClient, db_session: Session
) -> None:
    """Aggregating classifications into a number is a later phase."""
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id))

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/evidence"
    ).json()

    forbidden = {"demand_score", "score", "verdict", "whitespace_score"}
    assert not (set(body) & forbidden)


def test_evidence_counts_every_classification(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id))

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/evidence"
    ).json()

    assert body["counts"] == {"support": 1}


def test_evidence_reports_every_query_that_found_the_page(
    api_client: TestClient, db_session: Session
) -> None:
    """PROVENANCE. Convergence is reported as the queries, not as a score."""
    investigation_id = create(api_client)
    seed(
        db_session,
        uuid.UUID(investigation_id),
        queries=("cargo booking problems", "freight dispatch challenges"),
    )

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/evidence"
    ).json()

    provenance = body["evidence"][0]["provenance"]
    assert set(provenance["found_by_queries"]) == {
        "cargo booking problems",
        "freight dispatch challenges",
    }
    assert provenance["best_position"] == 2


def test_evidence_for_an_unknown_investigation_is_a_404(
    api_client: TestClient,
) -> None:
    unknown = uuid.uuid4()
    response = api_client.get(f"/api/v1/investigations/{unknown}/evidence")

    assert response.status_code == 404
    assert str(unknown) in response.json()["detail"]


@pytest.mark.parametrize("limit", [0, -1, 201])
def test_the_evidence_limit_is_bounded(api_client: TestClient, limit: int) -> None:
    investigation_id = create(api_client)
    response = api_client.get(
        f"/api/v1/investigations/{investigation_id}/evidence",
        params={"limit": limit},
    )
    assert response.status_code == 422


# -- competitors ------------------------------------------------------------


def test_competitors_for_a_never_run_investigation_is_empty_but_valid(
    api_client: TestClient,
) -> None:
    investigation_id = create(api_client)

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/competitors"
    ).json()

    assert set(body) == COMPETITOR_KEYS
    assert body["competitors"] == []


def test_competitors_have_exactly_the_documented_keys(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id), family="competitor")

    body = api_client.get(
        f"/api/v1/investigations/{investigation_id}/competitors"
    ).json()

    assert set(body["competitors"][0]) == COMPETITOR_ITEM_KEYS


def test_competitors_carry_no_pricing_or_feature_data(
    api_client: TestClient, db_session: Session
) -> None:
    """Discovery never opened the page, so it has no basis for either."""
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id), family="competitor")

    competitor = api_client.get(
        f"/api/v1/investigations/{investigation_id}/competitors"
    ).json()["competitors"][0]

    forbidden = {"pricing", "price", "features", "funding", "employees"}
    assert not (set(competitor) & forbidden)


def test_a_competitor_name_is_the_page_title(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id), family="competitor")

    competitor = api_client.get(
        f"/api/v1/investigations/{investigation_id}/competitors"
    ).json()["competitors"][0]

    assert competitor["name"] == "Cargo booking is manual"


def test_competitors_for_an_unknown_investigation_is_a_404(
    api_client: TestClient,
) -> None:
    assert (
        api_client.get(
            f"/api/v1/investigations/{uuid.uuid4()}/competitors"
        ).status_code
        == 404
    )


# -- the surfaces are separate ---------------------------------------------


def test_demand_and_competitor_surfaces_do_not_leak_into_each_other(
    api_client: TestClient, db_session: Session
) -> None:
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id), family="demand")

    competitors = api_client.get(
        f"/api/v1/investigations/{investigation_id}/competitors"
    ).json()

    assert competitors["competitors"] == []


def test_the_research_surface_is_unaffected_by_web_evidence(
    api_client: TestClient, db_session: Session
) -> None:
    """A web page is not a paper, and the research read model never sees one."""
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id))

    research = api_client.get(
        f"/api/v1/investigations/{investigation_id}/research"
    ).json()

    assert research["paper_count"] == 0
    assert research["top_papers"] == []


# -- discovery only ---------------------------------------------------------


def test_no_get_on_this_surface_contacts_a_provider(
    api_client: TestClient,
    refuse_openai: None,
    investigation_scheduler: RecordingScheduler,
    db_session: Session,
) -> None:
    """Including the discovered URLs, which are never opened on a read."""
    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id))

    for path in (
        f"/api/v1/investigations/{investigation_id}/evidence",
        f"/api/v1/investigations/{investigation_id}/competitors",
        f"/api/v1/investigations/{investigation_id}/research",
        f"/api/v1/investigations/{investigation_id}/run",
    ):
        assert api_client.get(path).status_code == 200, path

    assert investigation_scheduler.scheduled == []


def test_reading_evidence_starts_no_run(
    api_client: TestClient, db_session: Session
) -> None:
    from sqlalchemy import func, select

    from app.db.models import InvestigationRun

    investigation_id = create(api_client)
    seed(db_session, uuid.UUID(investigation_id))

    api_client.get(f"/api/v1/investigations/{investigation_id}/evidence")

    assert (
        db_session.execute(
            select(func.count()).select_from(InvestigationRun)
        ).scalar_one()
        == 0
    )


# -- run progress -----------------------------------------------------------


def test_the_run_endpoint_exposes_phase_progress(api_client: TestClient) -> None:
    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    body = api_client.get(f"/api/v1/investigations/{investigation_id}/run").json()

    assert set(body["phases"]) == {
        "planning",
        "research",
        "demand",
        "competitors",
    }
    assert body["phases"]["planning"]["state"] == "pending"
    assert body["phases"]["demand"]["queries_total"] == 0


def test_phase_progress_carries_no_whitespace_or_verdict(
    api_client: TestClient,
) -> None:
    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")

    body = api_client.get(f"/api/v1/investigations/{investigation_id}/run").json()

    assert "whitespace" not in body["phases"]
    assert "verdict" not in body["phases"]


def test_a_run_predating_phases_reads_as_pending(
    api_client: TestClient, db_session: Session
) -> None:
    """An old row stores "{}", which is honest for a run without phases."""
    from app.db.models import InvestigationRun

    investigation_id = create(api_client)
    api_client.post(f"/api/v1/investigations/{investigation_id}/run")
    run = db_session.execute(
        __import__("sqlalchemy").select(InvestigationRun)
    ).scalar_one()
    run.phases = {}
    db_session.commit()

    body = api_client.get(f"/api/v1/investigations/{investigation_id}/run").json()

    assert body["phases"]["research"]["state"] == "pending"
    assert body["phases"]["research"]["discovered"] == 0
