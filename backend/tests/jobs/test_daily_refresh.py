"""The production daily market refresh: what it claims, and what it costs.

NO TEST HERE MAY REACH A PROVIDER. The Bright Data client is replaced by
a spy that records every call and raises if anything asks it to start a
collection it was not told to expect, so a regression that reintroduced a
real trigger would fail these tests rather than quietly spend money.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Collector, PipelineRun, Source
from app.domain.enums import CollectorStatus, PipelineRunStatus, ReliabilityState
from app.jobs import daily_refresh
from app.jobs.daily_refresh import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_TEMPORARY,
    DailyRefreshConfigurationError,
    business_date,
    exit_code_for,
    idempotency_key_for,
    main,
    resolve_market_collector,
    run_daily_refresh,
)
from app.pipeline import executor as pipeline_executor
from tests.opportunity_engine.conftest import make_collector, make_source

PROD_COLLECTOR_ID = uuid.UUID("48cbf27f-8b29-4106-ba39-b812a0002694")
PROD_EXTERNAL_ID = "c_mswvtpby29tybc04dr"
TODAY = date(2026, 8, 20)
YESTERDAY = date(2026, 8, 19)


class ExplodingClient:
    """Any attribute access is a provider call, and every one is a failure.

    The daily refresh must reach Bright Data only through the pipeline
    executor, which these tests stub. If anything else touches the
    client, this makes it loud.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - guard
        raise AssertionError(f"the test suite must not call the provider: {name!r}")


class SpyDriver:
    """Stands in for drive_pipeline_run and records what it was asked to drive.

    Drives the run to `final_status` without any provider contact, which
    is what lets the entrypoint's claiming, idempotency and exit-code
    behaviour be tested independently of the pipeline itself.
    """

    def __init__(
        self,
        *,
        final_status: PipelineRunStatus = PipelineRunStatus.COMPLETED,
        trusted: bool | None = True,
        reliability: ReliabilityState | None = ReliabilityState.HEALTHY,
    ) -> None:
        self.final_status = final_status
        self.trusted = trusted
        self.reliability = reliability
        self.driven: list[uuid.UUID] = []

    def __call__(
        self, session: Session, client: Any, *, pipeline_run_id: uuid.UUID, **kw: Any
    ) -> PipelineRun:
        self.driven.append(pipeline_run_id)
        run = session.get(PipelineRun, pipeline_run_id)
        assert run is not None
        run.status = self.final_status
        run.trusted = self.trusted
        run.reliability_state = self.reliability
        # A real drive persists a provider job id on the way through.
        run.provider_job_id = run.provider_job_id or "j_test_collection"
        session.commit()
        session.refresh(run)
        return run


class SpyResumer:
    """Stands in for resume_unfinished_pipeline_runs."""

    def __init__(self, *, returns: list[PipelineRun] | None = None) -> None:
        self.calls = 0
        self._returns = returns or []

    def __call__(self, session: Session, client: Any, **kw: Any) -> list[PipelineRun]:
        self.calls += 1
        return self._returns


@pytest.fixture
def prod_collector(db_session: Session, source: Source) -> Collector:
    """A collector shaped exactly like the production market collector."""
    collector = Collector(
        id=PROD_COLLECTOR_ID,
        source_id=source.id,
        provider="brightdata",
        external_collector_id=PROD_EXTERNAL_ID,
        name="gapradar-fix-my-itch",
        status=CollectorStatus.ACTIVE,
    )
    db_session.add(collector)
    db_session.commit()
    db_session.refresh(collector)
    return collector


@pytest.fixture
def settings() -> Settings:
    return Settings(
        MARKET_COLLECTOR_ID=str(PROD_COLLECTOR_ID),
        MARKET_COLLECTOR_PROVIDER="brightdata",
        MARKET_COLLECTOR_EXTERNAL_ID=PROD_EXTERNAL_ID,
    )


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Replace the executor's waiting parts; keep its claiming parts real.

    `start_pipeline_run` is deliberately NOT stubbed -- the idempotency
    and active-run guarantees under test are its behaviour, and testing
    against a fake would prove nothing about the real thing.
    """

    def install(
        driver: SpyDriver, resumer: SpyResumer | None = None
    ) -> tuple[SpyDriver, SpyResumer]:
        resumer = resumer or SpyResumer()
        monkeypatch.setattr(daily_refresh, "drive_pipeline_run", driver)
        monkeypatch.setattr(daily_refresh, "resume_unfinished_pipeline_runs", resumer)
        return driver, resumer

    return install


def runs_for(session: Session, collector_id: uuid.UUID) -> list[PipelineRun]:
    return list(
        session.execute(
            select(PipelineRun)
            .where(PipelineRun.collector_id == collector_id)
            .order_by(PipelineRun.created_at)
        ).scalars()
    )


def run_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(PipelineRun)).scalar_one()


# -- collector resolution ---------------------------------------------------


def test_the_production_collector_is_resolved_and_verified(
    db_session: Session, prod_collector: Collector, settings: Settings
) -> None:
    resolved = resolve_market_collector(db_session, settings)

    assert resolved.id == PROD_COLLECTOR_ID
    assert resolved.external_collector_id == PROD_EXTERNAL_ID


def test_a_missing_collector_fails_clearly(
    db_session: Session, settings: Settings
) -> None:
    """No collector row: refuse loudly rather than scraping something else."""
    with pytest.raises(DailyRefreshConfigurationError, match="does not exist"):
        resolve_market_collector(db_session, settings)


def test_a_wrong_provider_fails_closed(
    db_session: Session, source: Source, settings: Settings
) -> None:
    db_session.add(
        Collector(
            id=PROD_COLLECTOR_ID,
            source_id=source.id,
            provider="some-other-provider",
            external_collector_id=PROD_EXTERNAL_ID,
            name="impostor",
            status=CollectorStatus.ACTIVE,
        )
    )
    db_session.commit()

    with pytest.raises(DailyRefreshConfigurationError, match="provider"):
        resolve_market_collector(db_session, settings)


def test_a_wrong_external_collector_id_fails_closed(
    db_session: Session, source: Source, settings: Settings
) -> None:
    """The id that decides which Bright Data collector actually runs."""
    db_session.add(
        Collector(
            id=PROD_COLLECTOR_ID,
            source_id=source.id,
            provider="brightdata",
            external_collector_id="c_something_else_entirely",
            name="wrong target",
            status=CollectorStatus.ACTIVE,
        )
    )
    db_session.commit()

    with pytest.raises(DailyRefreshConfigurationError, match="external collector"):
        resolve_market_collector(db_session, settings)


def test_a_paused_collector_is_not_refreshed(
    db_session: Session, source: Source, settings: Settings
) -> None:
    db_session.add(
        Collector(
            id=PROD_COLLECTOR_ID,
            source_id=source.id,
            provider="brightdata",
            external_collector_id=PROD_EXTERNAL_ID,
            name="paused",
            status=CollectorStatus.PAUSED,
        )
    )
    db_session.commit()

    with pytest.raises(DailyRefreshConfigurationError, match="not active"):
        resolve_market_collector(db_session, settings)


def test_a_malformed_collector_id_setting_fails_clearly(db_session: Session) -> None:
    bad = Settings(MARKET_COLLECTOR_ID="not-a-uuid")

    with pytest.raises(DailyRefreshConfigurationError, match="valid UUID"):
        resolve_market_collector(db_session, bad)


# -- one refresh per business day -------------------------------------------


def test_the_first_invocation_creates_exactly_one_logical_run(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    driver, _ = stub_pipeline(SpyDriver())

    run, already = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )

    assert already is False
    assert run.status is PipelineRunStatus.COMPLETED
    assert run.idempotency_key == idempotency_key_for(PROD_COLLECTOR_ID, TODAY)
    assert len(runs_for(db_session, PROD_COLLECTOR_ID)) == 1
    assert driver.driven == [run.id]


def test_a_second_invocation_the_same_day_does_not_start_another_execution(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    """A double-fired cron must not buy a second Bright Data collection."""
    driver, _ = stub_pipeline(SpyDriver())

    first, _ = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )
    driver.driven.clear()

    second, already = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )

    assert already is True
    assert second.id == first.id
    assert len(runs_for(db_session, PROD_COLLECTOR_ID)) == 1
    # Today is already terminal, so nothing was driven and nothing was
    # asked of the provider.
    assert driver.driven == []


def test_an_active_execution_is_joined_rather_than_duplicated(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    """An in-flight run is rejoined and driven, not re-triggered."""
    # A run left mid-flight by a previous process, holding a provider job.
    inflight = PipelineRun(
        collector_id=PROD_COLLECTOR_ID,
        status=PipelineRunStatus.WAITING_PROVIDER,
        provider_job_id="j_already_running",
        idempotency_key=idempotency_key_for(PROD_COLLECTOR_ID, TODAY),
    )
    db_session.add(inflight)
    db_session.commit()
    db_session.refresh(inflight)

    driver, _ = stub_pipeline(SpyDriver())

    run, already = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )

    assert already is True
    assert run.id == inflight.id
    # Rejoined and driven -- the same provider job, not a second one.
    assert driver.driven == [inflight.id]
    assert run.provider_job_id == "j_already_running"
    assert len(runs_for(db_session, PROD_COLLECTOR_ID)) == 1


def test_an_active_run_without_todays_key_still_blocks_a_second_execution(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    """The active-collector guarantee survives alongside the daily key.

    A manual run started from the API carries no idempotency key. The
    daily refresh must join it rather than run a competing collection
    over the same source.
    """
    manual = PipelineRun(
        collector_id=PROD_COLLECTOR_ID,
        status=PipelineRunStatus.WAITING_PROVIDER,
        provider_job_id="j_manual_operator_run",
    )
    db_session.add(manual)
    db_session.commit()
    db_session.refresh(manual)

    stub_pipeline(SpyDriver())

    run, already = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )

    assert already is True
    assert run.id == manual.id
    assert run.provider_job_id == "j_manual_operator_run"
    assert len(runs_for(db_session, PROD_COLLECTOR_ID)) == 1


def test_yesterdays_completed_refresh_does_not_block_today(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    stub_pipeline(SpyDriver())

    yesterday_run, _ = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=YESTERDAY
    )
    assert yesterday_run.status is PipelineRunStatus.COMPLETED

    today_run, already = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )

    assert already is False
    assert today_run.id != yesterday_run.id
    assert today_run.idempotency_key == idempotency_key_for(PROD_COLLECTOR_ID, TODAY)
    assert len(runs_for(db_session, PROD_COLLECTOR_ID)) == 2


def test_the_business_date_is_ist_not_utc() -> None:
    """00:30 UTC is already tomorrow in IST -- the key must reflect that.

    Getting this wrong would make the very first run of each day collide
    with the previous day's key and silently no-op.
    """
    from datetime import UTC, datetime

    # The moment the cron fires: 00:30 UTC on the 20th.
    fired = datetime(2026, 8, 20, 0, 30, tzinfo=UTC)

    assert business_date(lambda: fired) == date(2026, 8, 20)
    # And just before midnight UTC is still the same IST day.
    assert business_date(lambda: datetime(2026, 8, 19, 23, 0, tzinfo=UTC)) == date(
        2026, 8, 20
    )


def test_the_idempotency_key_is_scoped_to_the_collector() -> None:
    other = uuid.uuid4()

    assert idempotency_key_for(PROD_COLLECTOR_ID, TODAY) != idempotency_key_for(
        other, TODAY
    )


# -- recovery ---------------------------------------------------------------


def test_unfinished_work_is_resumed_before_anything_new_is_claimed(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    """Work already paid for at the provider is finished first."""
    _, resumer = stub_pipeline(SpyDriver())

    run_daily_refresh(db_session, ExplodingClient(), settings=settings, day=TODAY)

    assert resumer.calls == 1


def test_a_resume_failure_does_not_cost_todays_refresh(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Yesterday being stuck must not stop today from running."""

    def exploding_resume(*_: Any, **__: Any) -> list[PipelineRun]:
        raise RuntimeError("resume blew up")

    driver = SpyDriver()
    monkeypatch.setattr(daily_refresh, "drive_pipeline_run", driver)
    monkeypatch.setattr(
        daily_refresh, "resume_unfinished_pipeline_runs", exploding_resume
    )

    run, already = run_daily_refresh(
        db_session, ExplodingClient(), settings=settings, day=TODAY
    )

    assert already is False
    assert run.status is PipelineRunStatus.COMPLETED


# -- exit codes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (PipelineRunStatus.COMPLETED, EXIT_OK),
        # RecallGuard detecting a problem is the system working.
        (PipelineRunStatus.DEGRADED, EXIT_OK),
        (PipelineRunStatus.FAILED, EXIT_FAILED),
        # Still active: local patience ran out, execution is resumable.
        (PipelineRunStatus.WAITING_PROVIDER, EXIT_TEMPORARY),
        (PipelineRunStatus.QUEUED, EXIT_TEMPORARY),
    ],
)
def test_exit_code_mapping(
    db_session: Session,
    prod_collector: Collector,
    status: PipelineRunStatus,
    expected: int,
) -> None:
    run = PipelineRun(collector_id=PROD_COLLECTOR_ID, status=status)
    db_session.add(run)
    db_session.commit()

    assert exit_code_for(run) == expected


# -- the entrypoint ---------------------------------------------------------


@pytest.fixture
def factories(db_session: Session) -> tuple[Any, Any]:
    """Session and provider factories that never touch a real service."""

    @contextmanager
    def session_factory() -> Iterator[Session]:
        yield db_session

    @contextmanager
    def client_factory() -> Iterator[ExplodingClient]:
        yield ExplodingClient()

    return session_factory, client_factory


def test_a_successful_refresh_exits_zero(
    db_session: Session,
    prod_collector: Collector,
    factories: Any,
    stub_pipeline: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daily_refresh, "get_settings", lambda: Settings(APP_ENV="test")
    )
    stub_pipeline(SpyDriver())
    session_factory, client_factory = factories

    code = main(
        ["--business-date", TODAY.isoformat()],
        session_factory=session_factory,
        client_factory=client_factory,
    )

    assert code == EXIT_OK
    assert run_count(db_session) == 1


def test_a_failed_pipeline_exits_nonzero(
    db_session: Session,
    prod_collector: Collector,
    factories: Any,
    stub_pipeline: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daily_refresh, "get_settings", lambda: Settings(APP_ENV="test")
    )
    stub_pipeline(
        SpyDriver(
            final_status=PipelineRunStatus.FAILED, trusted=None, reliability=None
        )
    )
    session_factory, client_factory = factories

    code = main(
        ["--business-date", TODAY.isoformat()],
        session_factory=session_factory,
        client_factory=client_factory,
    )

    assert code == EXIT_FAILED


def test_local_budget_exhaustion_exits_temporary_and_leaves_work_resumable(
    db_session: Session,
    prod_collector: Collector,
    factories: Any,
    stub_pipeline: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Impatience is not failure: the run keeps its provider job."""
    monkeypatch.setattr(
        daily_refresh, "get_settings", lambda: Settings(APP_ENV="test")
    )
    stub_pipeline(
        SpyDriver(
            final_status=PipelineRunStatus.WAITING_PROVIDER,
            trusted=None,
            reliability=None,
        )
    )
    session_factory, client_factory = factories

    code = main(
        ["--business-date", TODAY.isoformat()],
        session_factory=session_factory,
        client_factory=client_factory,
    )

    assert code == EXIT_TEMPORARY
    run = runs_for(db_session, PROD_COLLECTOR_ID)[0]
    assert run.status is PipelineRunStatus.WAITING_PROVIDER
    assert run.provider_job_id is not None
    assert run.error is None


def test_a_degraded_result_exits_zero_without_softening_the_verdict(
    db_session: Session,
    prod_collector: Collector,
    factories: Any,
    stub_pipeline: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cron is green; the database still says DEGRADED and untrusted."""
    monkeypatch.setattr(
        daily_refresh, "get_settings", lambda: Settings(APP_ENV="test")
    )
    stub_pipeline(
        SpyDriver(
            final_status=PipelineRunStatus.DEGRADED,
            trusted=False,
            reliability=ReliabilityState.DEGRADED,
        )
    )
    session_factory, client_factory = factories

    code = main(
        ["--business-date", TODAY.isoformat()],
        session_factory=session_factory,
        client_factory=client_factory,
    )

    assert code == EXIT_OK
    run = runs_for(db_session, PROD_COLLECTOR_ID)[0]
    assert run.status is PipelineRunStatus.DEGRADED
    assert run.trusted is False
    assert run.reliability_state is ReliabilityState.DEGRADED


def test_a_misconfigured_deployment_exits_nonzero_and_claims_nothing(
    db_session: Session,
    factories: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No collector row at all: fail before spending anything."""
    monkeypatch.setattr(
        daily_refresh, "get_settings", lambda: Settings(APP_ENV="test")
    )
    session_factory, client_factory = factories

    code = main(
        ["--business-date", TODAY.isoformat()],
        session_factory=session_factory,
        client_factory=client_factory,
    )

    assert code == EXIT_FAILED
    # Nothing claimed, so nothing to clean up and nothing billed.
    assert run_count(db_session) == 0


def test_the_entrypoint_never_constructs_a_real_provider_client(
    db_session: Session,
    prod_collector: Collector,
    factories: Any,
    stub_pipeline: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole suite's guarantee, asserted explicitly.

    ExplodingClient raises on ANY attribute access, so reaching this
    assertion at all proves nothing in the entrypoint path touched the
    provider outside the stubbed executor.
    """
    monkeypatch.setattr(
        daily_refresh, "get_settings", lambda: Settings(APP_ENV="test")
    )
    stub_pipeline(SpyDriver())
    session_factory, client_factory = factories

    code = main(
        ["--business-date", TODAY.isoformat()],
        session_factory=session_factory,
        client_factory=client_factory,
    )

    assert code == EXIT_OK


def test_a_second_source_and_collector_are_untouched_by_the_refresh(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    stub_pipeline: Any,
) -> None:
    """The refresh drives ONE named collector, not everything active."""
    other_source = make_source(db_session, name="Unrelated source")
    other = make_collector(
        db_session,
        other_source,
        name="unrelated",
        external_collector_id="c_unrelated",
    )
    driver, _ = stub_pipeline(SpyDriver())

    run_daily_refresh(db_session, ExplodingClient(), settings=settings, day=TODAY)

    assert runs_for(db_session, other.id) == []
    assert len(driver.driven) == 1


# -- recovery is scoped to THIS collector -----------------------------------
#
# These tests deliberately do NOT stub resume_unfinished_pipeline_runs.
# The scoping bug they guard against was invisible to the tests above
# precisely because those replace the resumer: only the real selection
# query can prove which runs a market-only cron is willing to advance.


@pytest.fixture
def real_resume_stub_drive(monkeypatch: pytest.MonkeyPatch) -> SpyDriver:
    """Real recovery selection, fake driving.

    `drive_pipeline_run` is patched in BOTH modules: daily_refresh calls
    its own import for today's run, and the executor calls its module
    global from inside resume_unfinished_pipeline_runs. Patching one and
    not the other would let a real provider call escape.
    """
    driver = SpyDriver()
    monkeypatch.setattr(daily_refresh, "drive_pipeline_run", driver)
    monkeypatch.setattr(pipeline_executor, "drive_pipeline_run", driver)
    return driver


def make_unfinished_run(
    session: Session, collector_id: uuid.UUID, *, provider_job_id: str
) -> PipelineRun:
    run = PipelineRun(
        collector_id=collector_id,
        status=PipelineRunStatus.WAITING_PROVIDER,
        provider_job_id=provider_job_id,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def test_an_unfinished_run_for_the_market_collector_is_resumed(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    real_resume_stub_drive: SpyDriver,
) -> None:
    stranded = make_unfinished_run(
        db_session, PROD_COLLECTOR_ID, provider_job_id="j_market_stranded"
    )

    run_daily_refresh(db_session, ExplodingClient(), settings=settings, day=TODAY)

    assert stranded.id in real_resume_stub_drive.driven


def test_another_collectors_unfinished_run_is_never_resumed(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    real_resume_stub_drive: SpyDriver,
) -> None:
    """A market-only cron must not advance unrelated collector work.

    Resuming polls a provider job -- and a QUEUED run would be triggered
    outright -- so this is about spend and blast radius, not tidiness.
    """
    other_source = make_source(db_session, name="Unrelated source")
    other = make_collector(
        db_session,
        other_source,
        name="unrelated",
        external_collector_id="c_unrelated",
    )
    foreign = make_unfinished_run(
        db_session, other.id, provider_job_id="j_someone_elses_job"
    )

    run_daily_refresh(db_session, ExplodingClient(), settings=settings, day=TODAY)

    assert foreign.id not in real_resume_stub_drive.driven
    # And it is left exactly as it was: still in flight, still holding its
    # provider job, for whoever legitimately owns it.
    db_session.refresh(foreign)
    assert foreign.status is PipelineRunStatus.WAITING_PROVIDER
    assert foreign.provider_job_id == "j_someone_elses_job"


def test_only_the_market_collectors_work_is_driven_at_all(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    real_resume_stub_drive: SpyDriver,
) -> None:
    """Every run the cron touches must belong to the market collector."""
    other_source = make_source(db_session, name="Unrelated source")
    other = make_collector(
        db_session,
        other_source,
        name="unrelated",
        external_collector_id="c_unrelated",
    )
    make_unfinished_run(db_session, other.id, provider_job_id="j_other_one")
    make_unfinished_run(db_session, PROD_COLLECTOR_ID, provider_job_id="j_market_one")

    run_daily_refresh(db_session, ExplodingClient(), settings=settings, day=TODAY)

    driven_collectors = {
        db_session.get(PipelineRun, run_id).collector_id
        for run_id in real_resume_stub_drive.driven
    }
    assert driven_collectors == {PROD_COLLECTOR_ID}


def test_a_foreign_queued_run_is_never_triggered_by_the_refresh(
    db_session: Session,
    prod_collector: Collector,
    settings: Settings,
    real_resume_stub_drive: SpyDriver,
) -> None:
    """The worst case: QUEUED means resuming would START a collection.

    A foreign run with no provider job yet is the one where an unscoped
    resume does not merely poll someone else's work -- it buys a new
    Bright Data job on a collector this cron was never pointed at.
    """
    other_source = make_source(db_session, name="Unrelated source")
    other = make_collector(
        db_session,
        other_source,
        name="unrelated",
        external_collector_id="c_unrelated",
    )
    queued = PipelineRun(collector_id=other.id, status=PipelineRunStatus.QUEUED)
    db_session.add(queued)
    db_session.commit()
    db_session.refresh(queued)

    run_daily_refresh(db_session, ExplodingClient(), settings=settings, day=TODAY)

    assert queued.id not in real_resume_stub_drive.driven
    db_session.refresh(queued)
    # Never advanced, and crucially never given a provider job id.
    assert queued.status is PipelineRunStatus.QUEUED
    assert queued.provider_job_id is None
