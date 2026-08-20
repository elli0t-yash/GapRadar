"""A persisted web search names a family the engine actually runs.

Asserted at the DATABASE, not at the orchestration. `family` is a bare
VARCHAR written from WebSearchFamily.value, and the orchestration is not
the only writer a schema outlives -- a backfill script, a psql session,
or a future phase would all bypass it. These tests insert rows directly
through the ORM so what fails is the CHECK constraint itself.

Why it matters that this is enforced rather than assumed: every read of
this table is family-scoped. A row naming "demandd" is a paid provider
request that no phase counts, no progress reports, and no evidence hangs
off -- it would be invisible rather than wrong, which is worse.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Investigation, InvestigationWebSearchRun
from app.db.models.investigation_web import WEB_SEARCH_FAMILY_PREDICATE
from app.domain.enums import WebSearchStatus
from app.investigations.schemas import InvestigationCreate
from app.investigations.service import create_investigation
from app.web_intelligence.schemas import WebSearchFamily

SEARCHED_AT = datetime(2026, 8, 20, tzinfo=UTC)


@pytest.fixture
def subject_investigation(db_session: Session) -> Investigation:
    return create_investigation(
        db_session, payload=InvestigationCreate(query="cargo booking is broken")
    )


def insert(db_session: Session, investigation: Investigation, family: str) -> None:
    """Write one search run straight to the table and commit."""
    db_session.add(
        InvestigationWebSearchRun(
            investigation_id=investigation.id,
            family=family,
            query="cargo booking problems",
            provider="fake",
            product="fake_serp",
            locale_country="us",
            locale_language="en",
            status=WebSearchStatus.SUCCEEDED,
            records_returned=0,
        )
    )
    db_session.commit()


def search_runs(db_session: Session) -> int:
    return db_session.execute(
        select(func.count()).select_from(InvestigationWebSearchRun)
    ).scalar_one()


# -- accepted ---------------------------------------------------------------


def test_the_demand_family_is_accepted(
    db_session: Session, subject_investigation: Investigation
) -> None:
    insert(db_session, subject_investigation, WebSearchFamily.DEMAND.value)

    assert search_runs(db_session) == 1


def test_the_competitor_family_is_accepted(
    db_session: Session, subject_investigation: Investigation
) -> None:
    insert(db_session, subject_investigation, WebSearchFamily.COMPETITOR.value)

    assert search_runs(db_session) == 1


@pytest.mark.parametrize("family", list(WebSearchFamily))
def test_every_family_the_engine_runs_is_accepted(
    db_session: Session,
    subject_investigation: Investigation,
    family: WebSearchFamily,
) -> None:
    """Parametrised over the enum, so adding a member without widening
    the constraint fails here rather than in production."""
    insert(db_session, subject_investigation, family.value)

    assert search_runs(db_session) == 1


# -- rejected ---------------------------------------------------------------


@pytest.mark.parametrize(
    "family",
    [
        "research",  # a real family, but one that never uses SERP
        "whitespace",  # a phase that does not exist
        "demandd",  # a typo
        "DEMAND",  # the status columns' casing, which this column does not use
        "Demand",
        "",
        "demand,competitor",
    ],
)
def test_an_unsupported_family_is_rejected(
    db_session: Session, subject_investigation: Investigation, family: str
) -> None:
    with pytest.raises(IntegrityError):
        insert(db_session, subject_investigation, family)

    db_session.rollback()


@pytest.mark.parametrize("family", ["whitespace", "demandd", "DEMAND"])
def test_a_rejected_insert_leaves_no_row(
    db_session: Session, subject_investigation: Investigation, family: str
) -> None:
    """Nothing half-lands: the constraint fires on the INSERT itself."""
    with pytest.raises(IntegrityError):
        insert(db_session, subject_investigation, family)

    db_session.rollback()
    assert search_runs(db_session) == 0


def test_a_rejected_insert_does_not_disturb_an_accepted_one(
    db_session: Session, subject_investigation: Investigation
) -> None:
    insert(db_session, subject_investigation, WebSearchFamily.DEMAND.value)

    with pytest.raises(IntegrityError):
        insert(db_session, subject_investigation, "whitespace")
    db_session.rollback()

    assert search_runs(db_session) == 1


# -- the model and the migration agree --------------------------------------


def test_the_constraint_is_the_one_the_migration_creates() -> None:
    """Model and migration are written independently -- a migration keeps
    describing the schema it created even after the model moves on -- so
    the one thing worth pinning is that they have not diverged today."""
    revision = _load_revision(
        "alembic/versions/d4e17a92c5b8_create_investigation_web_intelligence.py"
    )

    model_predicate = next(
        str(constraint.sqltext)
        for constraint in InvestigationWebSearchRun.__table__.constraints
        if constraint.name == "ck_investigation_web_search_runs_family"
    )

    assert model_predicate == WEB_SEARCH_FAMILY_PREDICATE
    assert model_predicate == revision.WEB_SEARCH_FAMILY_PREDICATE


def test_the_predicate_names_exactly_the_enums_values() -> None:
    """Derived from the enum, so it cannot silently fall behind it."""
    for family in WebSearchFamily:
        assert f"'{family.value}'" in WEB_SEARCH_FAMILY_PREDICATE

    assert WEB_SEARCH_FAMILY_PREDICATE.count("'") == 2 * len(WebSearchFamily)


def test_the_predicate_is_lowercase_like_the_column_it_guards() -> None:
    """`family` stores WebSearchFamily.value, not the member NAME.

    The status columns beside it are SQLAlchemy Enums and store uppercase
    names; a predicate copied from their casing would reject every row.
    """
    assert "'demand'" in WEB_SEARCH_FAMILY_PREDICATE
    assert "'DEMAND'" not in WEB_SEARCH_FAMILY_PREDICATE


def _load_revision(relative_path: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
