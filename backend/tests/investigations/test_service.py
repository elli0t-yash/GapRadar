"""Persisting and reading investigations, with no provider anywhere near it."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Investigation, Signal
from app.domain.enums import InvestigationStatus
from app.investigations.schemas import InvestigationCreate
from app.investigations.service import (
    MAX_LIMIT,
    create_investigation,
    get_investigation,
    list_investigations,
)


def create(session: Session, query: str, **kwargs: object) -> Investigation:
    return create_investigation(
        session, payload=InvestigationCreate(query=query, **kwargs)
    )


def at(session: Session, investigation: Investigation, when: datetime) -> None:
    """Pin a row's created_at so ordering is asserted on facts, not clocks."""
    investigation.created_at = when
    session.commit()


def test_a_new_investigation_is_draft(db_session: Session) -> None:
    """Recorded, not started. Nothing has been investigated yet."""
    investigation = create(db_session, "clinics still fax referrals")
    assert investigation.status is InvestigationStatus.DRAFT


def test_a_new_investigation_is_persisted(db_session: Session) -> None:
    investigation = create(db_session, "clinics still fax referrals")

    db_session.expunge_all()
    stored = db_session.get(Investigation, investigation.id)

    assert stored is not None
    assert stored.query == "clinics still fax referrals"


def test_a_new_investigation_has_an_id_and_timestamps(db_session: Session) -> None:
    investigation = create(db_session, "clinics still fax referrals")

    assert isinstance(investigation.id, uuid.UUID)
    assert investigation.created_at is not None
    assert investigation.updated_at is not None


def test_the_stored_query_is_the_users_wording(db_session: Session) -> None:
    original = "Why do  SMB clinics STILL fax referrals?!"
    investigation = create(db_session, f"  {original}  ")
    assert investigation.query == original


def test_industry_is_optional(db_session: Session) -> None:
    """No industry means no industry -- never an invented one."""
    assert create(db_session, "rota swaps").industry is None


def test_industry_is_stored_when_given(db_session: Session) -> None:
    assert create(db_session, "rota swaps", industry="Healthcare").industry == (
        "Healthcare"
    )


def test_derived_fields_start_empty(db_session: Session) -> None:
    """Nothing produces a title or description yet, so neither is invented."""
    investigation = create(db_session, "rota swaps")
    assert investigation.title is None
    assert investigation.description is None


def test_creating_an_investigation_creates_no_signal(db_session: Session) -> None:
    """An investigation is a user hypothesis, not collected market evidence.

    Writing it into `signals` to reuse the research code would hand it
    trust it never earned: every consumer of that table reads it as data
    that survived collection, source-contract validation and RecallGuard.
    """
    create(db_session, "clinics still fax referrals")

    signals = db_session.execute(
        select(func.count()).select_from(Signal)
    ).scalar_one()
    assert signals == 0


def test_get_returns_the_investigation(db_session: Session) -> None:
    investigation = create(db_session, "rota swaps")
    found = get_investigation(db_session, investigation_id=investigation.id)
    assert found is not None
    assert found.id == investigation.id


def test_get_returns_none_for_an_unknown_id(db_session: Session) -> None:
    """None, not an exception: only the API layer decides that is a 404."""
    assert get_investigation(db_session, investigation_id=uuid.uuid4()) is None


def test_list_is_newest_first(db_session: Session) -> None:
    oldest = create(db_session, "oldest")
    middle = create(db_session, "middle")
    newest = create(db_session, "newest")
    at(db_session, oldest, datetime(2026, 8, 1, tzinfo=UTC))
    at(db_session, middle, datetime(2026, 8, 10, tzinfo=UTC))
    at(db_session, newest, datetime(2026, 8, 20, tzinfo=UTC))

    assert [i.query for i in list_investigations(db_session)] == [
        "newest",
        "middle",
        "oldest",
    ]


def test_list_ordering_is_stable_within_one_clock_tick(db_session: Session) -> None:
    """Two rows written at the same instant still have a total order.

    Without the id tie-break the database is free to return them in
    either order, and a paged read could show one twice and the other
    never.
    """
    same_moment = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    for query in ("a", "b", "c"):
        at(db_session, create(db_session, query), same_moment)

    first = [i.id for i in list_investigations(db_session)]
    second = [i.id for i in list_investigations(db_session)]
    assert first == second


def test_list_respects_the_requested_limit(db_session: Session) -> None:
    for index in range(5):
        create(db_session, f"query {index}")

    assert len(list_investigations(db_session, limit=2)) == 2


def test_list_is_bounded_even_when_asked_for_everything(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling lives in the service, not only in the route.

    A background job or a future caller that bypasses FastAPI's query
    validation must not be able to pull the whole table into memory.
    """
    monkeypatch.setattr("app.investigations.service.MAX_LIMIT", 2)
    for index in range(5):
        create(db_session, f"query {index}")

    assert len(list_investigations(db_session, limit=1_000_000)) == 2


def test_list_never_returns_nothing_for_a_nonsense_limit(db_session: Session) -> None:
    create(db_session, "rota swaps")
    assert len(list_investigations(db_session, limit=0)) == 1


def test_the_service_ceiling_is_sane() -> None:
    assert MAX_LIMIT >= 1
