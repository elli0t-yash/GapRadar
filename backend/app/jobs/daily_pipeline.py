"""Scheduler entrypoint: run the pipeline once for every active collector.

    uv run python -m app.jobs.daily_pipeline

Scheduling is deployment's job. There is no daemon, no queue, no broker,
and no 8 AM anywhere in this file -- cron (or any equivalent) decides
when this process starts, and the process decides nothing about when it
runs next.

Exit codes, for whatever is watching the cron job:

    0  every active collector was processed
    1  at least one collector raised an unexpected error

A collector that RecallGuard finds degraded is NOT an error: detecting
degradation and opening an incident is the system working. That outcome
is logged and summarized; the exit code stays 0 so a real crash remains
distinguishable from a real detection.
"""

import argparse
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Collector, Source
from app.db.session import get_session_factory
from app.domain.enums import CollectorStatus
from app.integrations.brightdata.client import BrightDataClient
from app.logging_config import configure_logging
from app.pipeline.executor import resume_unfinished_pipeline_runs
from app.pipeline.schemas import PipelineRunResult
from app.pipeline.service import baseline_from_history, run_pipeline

logger = logging.getLogger(__name__)


def active_collectors(session: Session) -> list[Collector]:
    """Collectors that are active on a source that is itself active.

    Both flags are checked: a paused collector and a retired source are
    different reasons to skip, and neither should be inferred from the
    other.
    """
    return list(
        session.execute(
            select(Collector)
            .join(Source, Collector.source_id == Source.id)
            .where(Collector.status == CollectorStatus.ACTIVE, Source.active.is_(True))
            .order_by(Collector.name)
        ).scalars()
    )


def run_daily_pipeline(
    session: Session,
    client: BrightDataClient,
    *,
    pipeline: Callable[..., PipelineRunResult] = run_pipeline,
) -> tuple[list[PipelineRunResult], list[tuple[Collector, Exception]]]:
    """Run every active collector through the pipeline, once each.

    One collector's unexpected failure does not stop the others: it is
    logged with its traceback, collected, and reported back so the caller
    can exit non-zero. Nothing is retried here -- a rerun is the
    scheduler's decision, and the repair budget is RecallGuard's.
    """
    # Anything a previous process left in flight is picked up first. The
    # API's local executor is in-process and does not survive a restart,
    # but the PipelineRun row and its Bright Data collection id do -- so
    # this rejoins that same collection rather than abandoning it or
    # starting a second one. It is deliberately before the fresh runs
    # below: finishing work already paid for at the provider comes first.
    try:
        resumed = resume_unfinished_pipeline_runs(session, client)
    # Reported, never hidden, and never allowed to stop the scheduled
    # work: a stuck execution from yesterday must not cost today's runs.
    except Exception:
        logger.exception("daily_pipeline_resume_failed")
    else:
        if resumed:
            logger.info(
                "daily_pipeline_resumed",
                extra={"pipeline_run_count": len(resumed)},
            )

    results: list[PipelineRunResult] = []
    failures: list[tuple[Collector, Exception]] = []

    collectors = active_collectors(session)
    if not collectors:
        logger.warning("daily_pipeline_no_active_collectors")

    for collector in collectors:
        try:
            result = pipeline(
                session,
                client,
                collector=collector,
                baseline=baseline_from_history(session, collector_id=collector.id),
            )
        # Reported, never hidden: logged with its traceback, returned to
        # the caller, and reflected in the exit code.
        except Exception as exc:
            logger.exception(
                "daily_pipeline_collector_failed",
                extra={"collector_id": str(collector.id)},
            )
            failures.append((collector, exc))
            continue

        results.append(result)
        logger.info(
            "daily_pipeline_collector_finished",
            extra={
                "collector_id": str(collector.id),
                "outcome": result.outcome.value,
                "reliability_state": result.reliability_state.value,
                "trusted": result.trusted,
                "incident_id": str(result.incident_id) if result.incident_id else None,
            },
        )

    return results, failures


@contextmanager
def _default_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def _default_client() -> Iterator[BrightDataClient]:
    with BrightDataClient() as client:
        yield client


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: Callable[[], AbstractContextManager[Session]] = _default_session,
    client_factory: Callable[
        [], AbstractContextManager[BrightDataClient]
    ] = _default_client,
    pipeline: Callable[..., PipelineRunResult] = run_pipeline,
) -> int:
    """Entrypoint. Returns the process exit code.

    The session and client factories are injected so the whole entrypoint
    can be tested against an in-memory database and a mock transport --
    this command must never make a real Bright Data call in a test.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.daily_pipeline",
        description=(
            "Run the GapRadar pipeline once for every active collector. "
            "Intended to be invoked by the deployment's scheduler."
        ),
    )
    parser.parse_args(argv)

    configure_logging(get_settings().APP_ENV)

    with session_factory() as session, client_factory() as client:
        results, failures = run_daily_pipeline(session, client, pipeline=pipeline)

    for result in results:
        print(
            f"collector={result.collector_id} outcome={result.outcome.value} "
            f"state={result.reliability_state.value} trusted={result.trusted}"
        )
    for collector, exc in failures:
        print(f"collector={collector.id} error={type(exc).__name__}: {exc}")

    print(f"collectors={len(results) + len(failures)} failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
