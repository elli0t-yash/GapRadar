"""The independent-investigation API surface.

Two properties matter more than the rest and are asserted directly:
creating an investigation contacts NO provider, and it writes no Signal.
The first keeps a text box from being able to spend money; the second
keeps a user hypothesis out of the table that means "validated market
evidence".
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Investigation, Signal
from app.investigations.schemas import MAX_QUERY_CHARS
from app.investigations.service import MAX_LIMIT
from tests.api.conftest import RecordingScheduler


def post(client: TestClient, **payload: object) -> dict[str, object]:
    response = client.post("/api/v1/investigations", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_creating_an_investigation_returns_it(api_client: TestClient) -> None:
    body = post(api_client, query="Clinics still fax referrals", industry="Healthcare")

    assert uuid.UUID(body["id"])
    assert body["query"] == "Clinics still fax referrals"
    assert body["industry"] == "Healthcare"
    assert body["status"] == "draft"
    assert body["title"] is None
    assert body["description"] is None
    assert body["created_at"] and body["updated_at"]


def test_a_created_investigation_is_readable_back(api_client: TestClient) -> None:
    created = post(api_client, query="Clinics still fax referrals")

    response = api_client.get(f"/api/v1/investigations/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_creating_an_investigation_starts_nothing(
    api_client: TestClient,
    scheduler: RecordingScheduler,
    enrichment_scheduler: RecordingScheduler,
) -> None:
    """201, not 202. Nothing was queued, so nothing may be promised.

    The Bright Data dependency on this client refuses every call, so a
    provider request here fails the test rather than reaching the
    network; the schedulers prove no background work was handed off
    either.
    """
    post(api_client, query="Clinics still fax referrals")

    assert scheduler.scheduled == []
    assert enrichment_scheduler.scheduled == []


def test_creating_an_investigation_makes_no_llm_call(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a question must never cost an LLM call."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected OpenAI client construction")

    monkeypatch.setattr("openai.OpenAI", refuse)

    post(api_client, query="Clinics still fax referrals")


def test_creating_an_investigation_creates_no_signal(
    api_client: TestClient, db_session: Session
) -> None:
    """`signals` means collected, validated evidence. This is not that."""
    post(api_client, query="Clinics still fax referrals")

    assert (
        db_session.execute(select(func.count()).select_from(Signal)).scalar_one() == 0
    )
    assert (
        db_session.execute(
            select(func.count()).select_from(Investigation)
        ).scalar_one()
        == 1
    )


@pytest.mark.parametrize("query", ["", "   ", "\n\t "])
def test_a_blank_query_is_rejected(api_client: TestClient, query: str) -> None:
    assert api_client.post(
        "/api/v1/investigations", json={"query": query}
    ).status_code == 422


def test_an_enormous_query_is_rejected(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/investigations", json={"query": "x" * (MAX_QUERY_CHARS + 1)}
    )
    assert response.status_code == 422


def test_a_missing_query_is_rejected(api_client: TestClient) -> None:
    assert api_client.post("/api/v1/investigations", json={}).status_code == 422


def test_outer_whitespace_is_trimmed_before_storing(api_client: TestClient) -> None:
    assert post(api_client, query="  rota swaps  ")["query"] == "rota swaps"


def test_industry_is_optional(api_client: TestClient) -> None:
    assert post(api_client, query="rota swaps")["industry"] is None


def test_an_unknown_investigation_is_a_404(api_client: TestClient) -> None:
    unknown = uuid.uuid4()
    response = api_client.get(f"/api/v1/investigations/{unknown}")

    assert response.status_code == 404
    assert str(unknown) in response.json()["detail"]


def test_a_malformed_investigation_id_is_a_422(api_client: TestClient) -> None:
    assert api_client.get("/api/v1/investigations/not-a-uuid").status_code == 422


def test_the_list_is_newest_first(
    api_client: TestClient, db_session: Session
) -> None:
    ids = [
        post(api_client, query=query)["id"] for query in ("oldest", "middle", "newest")
    ]
    for identifier, when in zip(
        ids,
        (
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 10, tzinfo=UTC),
            datetime(2026, 8, 20, tzinfo=UTC),
        ),
        strict=True,
    ):
        row = db_session.get(Investigation, uuid.UUID(identifier))
        assert row is not None
        row.created_at = when
    db_session.commit()

    body = api_client.get("/api/v1/investigations").json()

    assert [item["query"] for item in body] == ["newest", "middle", "oldest"]


def test_the_list_respects_a_limit(api_client: TestClient) -> None:
    for index in range(3):
        post(api_client, query=f"query {index}")

    body = api_client.get("/api/v1/investigations", params={"limit": 2}).json()

    assert len(body) == 2


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
def test_the_list_limit_is_bounded(api_client: TestClient, limit: int) -> None:
    """An unbounded list endpoint is one request away from a table scan."""
    response = api_client.get("/api/v1/investigations", params={"limit": limit})
    assert response.status_code == 422


def test_the_list_is_empty_before_anything_is_created(
    api_client: TestClient,
) -> None:
    assert api_client.get("/api/v1/investigations").json() == []
