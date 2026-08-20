"""EVERY research_search_run belongs to EXACTLY ONE subject.

Asserted at the DATABASE, not at the service. `ingest_arxiv_search_results`
refuses a bad pair too, but the service is not the only writer a schema
outlives: a backfill script, a psql session, or a future ingestion path
would all bypass it. These tests insert rows directly through the ORM so
what fails is the CHECK constraint itself.

All four combinations are covered, because the pair only means something
as a set: proving "both is rejected" says nothing about "neither".
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Investigation, ResearchSearchRun, Signal
from app.investigations.schemas import InvestigationCreate
from app.investigations.service import create_investigation

SEARCHED_AT = datetime(2026, 8, 20, tzinfo=UTC)


@pytest.fixture
def subject_investigation(db_session: Session) -> Investigation:
    return create_investigation(
        db_session, payload=InvestigationCreate(query="clinics still fax referrals")
    )


def insert(db_session: Session, **columns: object) -> None:
    """Write one search run straight to the table and commit."""
    db_session.add(
        ResearchSearchRun(query="cargo vehicle booking", searched_at=SEARCHED_AT, **columns)
    )
    db_session.commit()


def search_runs(db_session: Session) -> int:
    return db_session.execute(
        select(func.count()).select_from(ResearchSearchRun)
    ).scalar_one()


def test_a_signal_only_search_is_accepted(
    db_session: Session, opportunity_signal: Signal
) -> None:
    insert(db_session, signal_id=opportunity_signal.id, investigation_id=None)

    assert search_runs(db_session) == 1


def test_an_investigation_only_search_is_accepted(
    db_session: Session, subject_investigation: Investigation
) -> None:
    insert(db_session, signal_id=None, investigation_id=subject_investigation.id)

    assert search_runs(db_session) == 1


def test_a_search_naming_both_subjects_is_rejected(
    db_session: Session,
    opportunity_signal: Signal,
    subject_investigation: Investigation,
) -> None:
    """Two subjects makes "which problem was this searched for" unanswerable."""
    with pytest.raises(IntegrityError):
        insert(
            db_session,
            signal_id=opportunity_signal.id,
            investigation_id=subject_investigation.id,
        )

    db_session.rollback()
    assert search_runs(db_session) == 0


def test_a_search_naming_no_subject_is_rejected(db_session: Session) -> None:
    """THE CASE THE WEAKER CONSTRAINT LET THROUGH.

    A row with neither column set is a provider call that happened and
    that nothing accounts for: no read model can surface it, and no
    operator can say what it was for.
    """
    with pytest.raises(IntegrityError):
        insert(db_session, signal_id=None, investigation_id=None)

    db_session.rollback()
    assert search_runs(db_session) == 0


def test_omitting_both_columns_entirely_is_also_rejected(
    db_session: Session,
) -> None:
    """Not passing the columns must not be a way around not setting them."""
    with pytest.raises(IntegrityError):
        insert(db_session)

    db_session.rollback()
    assert search_runs(db_session) == 0


def test_the_constraint_is_the_one_the_migration_creates() -> None:
    """The model and the migration must describe the same rule.

    They are written independently -- a migration keeps describing the
    schema it created even after the model moves on -- so the one thing
    worth pinning is that they have not silently diverged today.
    """
    # Loaded by path: `alembic` on sys.path is the installed library, not
    # this project's migration directory.
    revision = _load_revision(
        "alembic/versions/c7b93e5a1d60_create_investigation_runs_and_research.py"
    )
    SINGLE_SUBJECT_PREDICATE = revision.SINGLE_SUBJECT_PREDICATE

    model_predicate = next(
        str(constraint.sqltext)
        for constraint in ResearchSearchRun.__table__.constraints
        if constraint.name == "ck_research_search_runs_single_subject"
    )

    assert model_predicate == SINGLE_SUBJECT_PREDICATE


def _load_revision(relative_path: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
