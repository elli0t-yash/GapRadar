"""The scheduler entrypoint: which collectors run, and what the exit code means."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.models import Collector, Source
from app.domain.enums import CollectorStatus, ReliabilityState
from app.jobs.daily_pipeline import active_collectors, main, run_daily_pipeline
from app.pipeline.schemas import PipelineOutcome, PipelineRunResult
from tests.opportunity_engine.conftest import make_collector, make_source


class SpyPipeline:
    """Stands in for run_pipeline and records which collectors it ran."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.collectors: list[Collector] = []
        self.fail_for = fail_for or set()

    def __call__(
        self, session: Session, client: Any, *, collector: Collector, **kwargs: Any
    ) -> PipelineRunResult:
        self.collectors.append(collector)
        if collector.name in self.fail_for:
            raise RuntimeError(f"provider exploded for {collector.name}")
        return PipelineRunResult(
            collector_id=collector.id,
            outcome=PipelineOutcome.HEALTHY,
            reliability_state=ReliabilityState.HEALTHY,
            trusted=True,
        )


@pytest.fixture
def factories(db_session: Session) -> tuple[Callable[[], Any], Callable[[], Any]]:
    """Session and provider factories that never touch a real service."""

    @contextmanager
    def session_factory() -> Iterator[Session]:
        yield db_session

    @contextmanager
    def client_factory() -> Iterator[None]:
        yield None

    return session_factory, client_factory


def test_only_active_collectors_on_active_sources_are_resolved(
    db_session: Session, source: Source
) -> None:
    active = make_collector(db_session, source, name="active")
    make_collector(
        db_session,
        source,
        name="paused",
        external_collector_id="c_paused",
        status=CollectorStatus.PAUSED,
    )

    retired = make_source(db_session, name="Retired source")
    retired.active = False
    db_session.commit()
    make_collector(
        db_session, retired, name="retired", external_collector_id="c_retired"
    )

    assert [c.id for c in active_collectors(db_session)] == [active.id]


def test_every_active_collector_is_run_once(
    db_session: Session, source: Source, collector: Collector
) -> None:
    second = make_collector(
        db_session, source, name="second", external_collector_id="c_second"
    )
    pipeline = SpyPipeline()

    results, failures = run_daily_pipeline(db_session, None, pipeline=pipeline)  # type: ignore[arg-type]

    assert {c.id for c in pipeline.collectors} == {collector.id, second.id}
    assert len(results) == 2
    assert failures == []


def test_the_entrypoint_exits_zero_when_every_collector_ran(
    factories: tuple[Callable[[], Any], Callable[[], Any]], collector: Collector
) -> None:
    session_factory, client_factory = factories

    exit_code = main(
        [],
        session_factory=session_factory,
        client_factory=client_factory,
        pipeline=SpyPipeline(),
    )

    assert exit_code == 0


def test_a_degraded_collector_is_not_an_error(
    factories: tuple[Callable[[], Any], Callable[[], Any]], collector: Collector
) -> None:
    """Detecting degradation is the system working, not the job failing."""
    session_factory, client_factory = factories

    def degraded(
        session: Session, client: Any, *, collector: Collector, **kwargs: Any
    ) -> PipelineRunResult:
        return PipelineRunResult(
            collector_id=collector.id,
            outcome=PipelineOutcome.DEGRADED,
            reliability_state=ReliabilityState.DEGRADED,
            trusted=False,
        )

    assert (
        main(
            [],
            session_factory=session_factory,
            client_factory=client_factory,
            pipeline=degraded,
        )
        == 0
    )


def test_an_unexpected_failure_exits_non_zero_and_is_surfaced(
    db_session: Session,
    factories: tuple[Callable[[], Any], Callable[[], Any]],
    source: Source,
    collector: Collector,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One collector's crash neither stops the others nor passes silently."""
    make_collector(db_session, source, name="broken", external_collector_id="c_broken")
    session_factory, client_factory = factories
    pipeline = SpyPipeline(fail_for={"broken"})

    exit_code = main(
        [],
        session_factory=session_factory,
        client_factory=client_factory,
        pipeline=pipeline,
    )

    assert exit_code == 1
    assert len(pipeline.collectors) == 2
    output = capsys.readouterr().out
    assert "RuntimeError: provider exploded for broken" in output
    assert "failures=1" in output


def test_no_active_collectors_is_reported_rather_than_crashing(
    factories: tuple[Callable[[], Any], Callable[[], Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_factory, client_factory = factories

    exit_code = main(
        [],
        session_factory=session_factory,
        client_factory=client_factory,
        pipeline=SpyPipeline(),
    )

    assert exit_code == 0
    assert "collectors=0 failures=0" in capsys.readouterr().out
