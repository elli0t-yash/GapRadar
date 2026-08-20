"""Claiming an investigation run: once, and only once.

The property under test is the one that costs money if it breaks. A
double-click, two tabs, or a re-rendered effect must not buy two sets of
Bright Data searches, and the guarantee has to survive two requests that
both looked before either wrote.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Investigation, InvestigationRun
from app.domain.enums import (
    InvestigationRunStatus,
    InvestigationStatus,
    ResearchOutcomeReason,
)
from app.investigations.runs import (
    RUN_STATUS_TO_INVESTIGATION_STATUS,
    active_run,
    latest_run,
    reconcile_stale_investigation_runs,
    set_run_status,
    start_run,
)

# Anchored to the real clock, not a fixed literal: rows written during a
# test get their created_at from the database's own now(), and a
# hard-coded reference in the future would make every fresh run look
# stale.
NOW = datetime.now(UTC)


def at(when: datetime):
    return lambda: when


def run_count(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(InvestigationRun)
    ).scalar_one()


# -- claiming ---------------------------------------------------------------


def test_a_run_starts_queued(db_session: Session, investigation: Investigation) -> None:
    """Claimed, not started. Nothing has been searched when this returns."""
    run, already_running = start_run(db_session, investigation=investigation)

    assert run.status is InvestigationRunStatus.QUEUED
    assert already_running is False
    assert run.started_at is None


def test_a_second_start_returns_the_run_already_in_flight(
    db_session: Session, investigation: Investigation
) -> None:
    first, _ = start_run(db_session, investigation=investigation)
    second, already_running = start_run(db_session, investigation=investigation)

    assert second.id == first.id
    assert already_running is True
    assert run_count(db_session) == 1


def test_the_database_refuses_a_second_active_run(
    db_session: Session, investigation: Investigation
) -> None:
    """THE GUARANTEE, not the lookup.

    Two concurrent requests can both observe "nothing running" before
    either inserts. `start_run`'s SELECT cannot prevent that; the partial
    unique index can, and this inserts underneath the service to prove
    the index -- not the Python -- is what fails the loser.
    """
    start_run(db_session, investigation=investigation)

    db_session.add(
        InvestigationRun(
            investigation_id=investigation.id, status=InvestigationRunStatus.QUEUED
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_a_new_run_is_allowed_once_the_previous_one_is_terminal(
    db_session: Session, investigation: Investigation
) -> None:
    """The index is PARTIAL, so history is unbounded."""
    first, _ = start_run(db_session, investigation=investigation)
    set_run_status(db_session, first, InvestigationRunStatus.SUCCEEDED)
    db_session.commit()

    second, already_running = start_run(db_session, investigation=investigation)

    assert already_running is False
    assert second.id != first.id
    assert run_count(db_session) == 2


def test_two_investigations_each_get_their_own_run(
    db_session: Session, investigation: Investigation
) -> None:
    from app.investigations.schemas import InvestigationCreate
    from app.investigations.service import create_investigation

    other = create_investigation(
        db_session, payload=InvestigationCreate(query="warehouse picking is manual")
    )

    start_run(db_session, investigation=investigation)
    _, already_running = start_run(db_session, investigation=other)

    assert already_running is False
    assert run_count(db_session) == 2


def test_claiming_makes_no_provider_call(
    db_session: Session, investigation: Investigation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lookup and one INSERT. Nothing else."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected provider client construction")

    monkeypatch.setattr("openai.OpenAI", refuse)
    monkeypatch.setattr(
        "app.integrations.brightdata.client.BrightDataClient.__init__", refuse
    )

    start_run(db_session, investigation=investigation)


# -- lookups ----------------------------------------------------------------


def test_active_run_is_none_before_anything_is_claimed(
    db_session: Session, investigation: Investigation
) -> None:
    assert active_run(db_session, investigation_id=investigation.id) is None


def test_latest_run_reports_terminal_runs_too(
    db_session: Session, investigation: Investigation
) -> None:
    """"Nothing active" and "nothing ever ran" are different facts."""
    run, _ = start_run(db_session, investigation=investigation)
    set_run_status(db_session, run, InvestigationRunStatus.FAILED)
    db_session.commit()

    assert active_run(db_session, investigation_id=investigation.id) is None
    latest = latest_run(db_session, investigation_id=investigation.id)
    assert latest is not None and latest.id == run.id


def test_latest_run_is_none_for_an_investigation_never_run(
    db_session: Session, investigation: Investigation
) -> None:
    assert latest_run(db_session, investigation_id=investigation.id) is None


# -- the Investigation.status mirror ----------------------------------------


@pytest.mark.parametrize(
    ("run_status", "expected"),
    sorted(RUN_STATUS_TO_INVESTIGATION_STATUS.items(), key=lambda kv: kv[0].value),
)
def test_every_run_status_maps_onto_the_investigation(
    db_session: Session,
    investigation: Investigation,
    run_status: InvestigationRunStatus,
    expected: InvestigationStatus,
) -> None:
    """The mapping is TOTAL: no run state leaves the subject undefined."""
    run, _ = start_run(db_session, investigation=investigation)

    set_run_status(db_session, run, run_status)
    db_session.commit()
    db_session.refresh(investigation)

    assert investigation.status is expected


def test_claiming_moves_the_investigation_out_of_draft(
    db_session: Session, investigation: Investigation
) -> None:
    """QUEUED means READY, not RUNNING.

    Nothing is executing yet, and claiming otherwise would report a state
    the backend cannot prove it is in.
    """
    assert investigation.status is InvestigationStatus.DRAFT

    start_run(db_session, investigation=investigation)
    db_session.refresh(investigation)

    assert investigation.status is InvestigationStatus.READY


def test_the_investigation_is_never_running_without_an_active_run(
    db_session: Session, investigation: Investigation
) -> None:
    """The invariant §6 warns about, asserted directly at every step."""
    run, _ = start_run(db_session, investigation=investigation)

    for status in (
        InvestigationRunStatus.RUNNING,
        InvestigationRunStatus.SUCCEEDED,
    ):
        set_run_status(db_session, run, status)
        db_session.commit()
        db_session.refresh(investigation)

        is_running = investigation.status is InvestigationStatus.RUNNING
        has_active = active_run(db_session, investigation_id=investigation.id)
        assert is_running == (has_active is not None)


# -- reconciliation ---------------------------------------------------------


def test_a_stranded_run_is_aged_out(
    db_session: Session, investigation: Investigation
) -> None:
    """What happens when the backend restarts mid-investigation."""
    run, _ = start_run(db_session, investigation=investigation)
    set_run_status(db_session, run, InvestigationRunStatus.RUNNING)
    run.created_at = NOW - timedelta(hours=2)
    db_session.commit()

    reconciled = reconcile_stale_investigation_runs(db_session, now=at(NOW))

    db_session.refresh(run)
    assert reconciled == 1
    assert run.status is InvestigationRunStatus.FAILED
    assert run.completed_at is not None


def test_a_reconciled_run_is_reported_as_retryable(
    db_session: Session, investigation: Investigation
) -> None:
    """The prose says "can be retried"; the machine-readable flag must agree.

    A stranded run whose outcome_reason stayed null would render a
    message promising a retry next to a UI that offers no button.
    """
    run, _ = start_run(db_session, investigation=investigation)
    run.created_at = NOW - timedelta(hours=2)
    db_session.commit()

    reconcile_stale_investigation_runs(db_session, now=at(NOW))

    db_session.refresh(run)
    assert run.outcome_reason is ResearchOutcomeReason.INTERRUPTED
    assert run.outcome_reason.is_retryable


def test_reconciliation_unblocks_the_investigation(
    db_session: Session, investigation: Investigation
) -> None:
    """Without this a stuck row disables the feature for that subject forever."""
    run, _ = start_run(db_session, investigation=investigation)
    run.created_at = NOW - timedelta(hours=2)
    db_session.commit()

    reconcile_stale_investigation_runs(db_session, now=at(NOW))
    fresh, already_running = start_run(db_session, investigation=investigation)

    assert already_running is False
    assert fresh.id != run.id


def test_reconciliation_mirrors_onto_the_investigation(
    db_session: Session, investigation: Investigation
) -> None:
    """A crash must not leave the subject reading RUNNING forever."""
    run, _ = start_run(db_session, investigation=investigation)
    set_run_status(db_session, run, InvestigationRunStatus.RUNNING)
    run.created_at = NOW - timedelta(hours=2)
    db_session.commit()

    reconcile_stale_investigation_runs(db_session, now=at(NOW))
    db_session.refresh(investigation)

    assert investigation.status is InvestigationStatus.FAILED


def test_an_in_flight_run_is_never_reconciled(
    db_session: Session, investigation: Investigation
) -> None:
    """Only rows older than the budget. A slow run is not a dead one."""
    run, _ = start_run(db_session, investigation=investigation)
    set_run_status(db_session, run, InvestigationRunStatus.RUNNING)
    db_session.commit()

    assert reconcile_stale_investigation_runs(db_session, now=at(NOW)) == 0
    db_session.refresh(run)
    assert run.status is InvestigationRunStatus.RUNNING


def test_reconciliation_leaves_terminal_runs_alone(
    db_session: Session, investigation: Investigation
) -> None:
    run, _ = start_run(db_session, investigation=investigation)
    set_run_status(db_session, run, InvestigationRunStatus.SUCCEEDED)
    run.created_at = NOW - timedelta(days=30)
    db_session.commit()

    assert reconcile_stale_investigation_runs(db_session, now=at(NOW)) == 0
    db_session.refresh(run)
    assert run.status is InvestigationRunStatus.SUCCEEDED


def test_reconciliation_does_not_touch_other_investigations(
    db_session: Session, investigation: Investigation
) -> None:
    from app.investigations.schemas import InvestigationCreate
    from app.investigations.service import create_investigation

    other = create_investigation(
        db_session, payload=InvestigationCreate(query="warehouse picking is manual")
    )
    stale, _ = start_run(db_session, investigation=investigation)
    stale.created_at = NOW - timedelta(hours=2)
    db_session.commit()
    fresh, _ = start_run(db_session, investigation=other)

    reconcile_stale_investigation_runs(db_session, now=at(NOW))

    db_session.refresh(fresh)
    assert fresh.status is InvestigationRunStatus.QUEUED


def test_reconciling_an_unknown_investigation_is_a_no_op(
    db_session: Session,
) -> None:
    assert latest_run(db_session, investigation_id=uuid.uuid4()) is None
    assert reconcile_stale_investigation_runs(db_session, now=at(NOW)) == 0
