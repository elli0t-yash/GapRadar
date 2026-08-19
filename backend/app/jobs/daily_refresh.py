"""One-shot production entrypoint: refresh the Fix My Itch market dataset.

    python -m app.jobs.daily_refresh

Intended to be a Railway Cron service running at 30 0 * * * (UTC), which
is 06:00 Asia/Kolkata. The process claims today's refresh, drives it, and
EXITS. There is no scheduler here, no daemon, no event loop and no web
server -- cron decides when this starts, and this decides nothing about
when it runs next.

WHY THIS EXISTS ALONGSIDE app.jobs.daily_pipeline. That job runs EVERY
active collector once, with no idempotency key: it is the "process
everything" job. This one drives exactly ONE named collector, once per
IST business day, and is safe to invoke twice. Pointing the production
cron at the every-collector job would make the daily market refresh
depend on which collectors happen to be active, which is not a property
anyone wants to discover during an incident.

NOTHING HERE IS A SECOND PIPELINE. Claiming, provider triggering,
validation, ingestion, RecallGuard and resume all belong to
app.pipeline.executor; this module only decides WHICH collector, WHICH
logical run, and WHAT the exit code means.

Exit codes, for whatever is watching the cron:

    0   today's refresh reached a terminal verdict (COMPLETED or
        DEGRADED), or today's refresh was already done and this
        invocation was correctly a no-op
    1   an operational failure: the run FAILED, production configuration
        is missing or wrong, or something raised
    75  EX_TEMPFAIL -- the local driver budget expired with the execution
        left resumable. Not a failure and not a success: Bright Data is
        still working, the provider job id is persisted, and the next
        invocation rejoins it. Distinct from 1 so a recurring timeout is
        visible without being mistaken for a broken pipeline.

DEGRADED IS EXIT 0 ON PURPOSE. RecallGuard judging a dataset
untrustworthy is the system working, and it is recorded as such in the
database. Exiting non-zero for it would page someone for a successful
detection, and would make a real crash indistinguishable from a real
finding. The verdict is logged and printed; nothing is softened to make
the cron look green.
"""

import argparse
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Collector, PipelineRun
from app.db.session import get_engine, get_session_factory
from app.domain.enums import CollectorStatus, PipelineRunStatus
from app.integrations.brightdata.client import BrightDataClient
from app.logging_config import configure_logging
from app.pipeline.executor import (
    DEFAULT_DRIVE_INTERVAL_SECONDS,
    DEFAULT_DRIVE_TIMEOUT_SECONDS,
    TERMINAL_STATUSES,
    drive_pipeline_run,
    resume_unfinished_pipeline_runs,
    start_pipeline_run,
)

logger = logging.getLogger(__name__)

# The schedule is expressed in IST even though cron fires in UTC, so the
# business date has to be IST too. Deriving it from the server clock's
# local date would put a 00:30 UTC run on the PREVIOUS day and make the
# idempotency key collide with yesterday's -- turning the first refresh
# of the day into a no-op.
BUSINESS_TIMEZONE = ZoneInfo("Asia/Kolkata")

EXIT_OK = 0
EXIT_FAILED = 1
# sysexits.h EX_TEMPFAIL: the work did not complete, but nothing is
# broken and retrying later is the correct response.
EXIT_TEMPORARY = 75


class DailyRefreshConfigurationError(Exception):
    """Production configuration is missing or does not match expectations.

    Raised BEFORE anything is claimed and before any provider call, so a
    misconfigured deployment costs nothing and changes nothing.
    """


def business_date(now: Callable[[], datetime] | None = None) -> date:
    """Today, in the timezone the schedule is written in."""
    moment = now() if now is not None else datetime.now(BUSINESS_TIMEZONE)
    return moment.astimezone(BUSINESS_TIMEZONE).date()


def idempotency_key_for(collector_id: uuid.UUID, day: date) -> str:
    """The key identifying ONE logical refresh of ONE collector on ONE day.

    The collector id is part of the key, not just the date. The executor
    resolves an idempotency key BEFORE it looks at the collector, so a
    date-only key would hand back the wrong collector's run if
    MARKET_COLLECTOR_ID were ever repointed between two invocations on
    the same day -- and it would do so silently.
    """
    return f"market-daily-refresh:{collector_id}:{day.isoformat()}"


def resolve_market_collector(
    session: Session, settings: Settings | None = None
) -> Collector:
    """Load the production market collector and prove it is the right one.

    Verification is deliberately paranoid and deliberately READ-ONLY.
    This process triggers a real, billable scrape against whatever it
    finds here, so "the row exists" is not enough -- it must be the
    Bright Data collector we expect, and it must be active. Nothing is
    created, and nothing is repaired: a daily job that fixed its own
    configuration would convert a deployment mistake into silent
    scraping of the wrong source.
    """
    settings = settings or get_settings()

    try:
        collector_id = uuid.UUID(settings.MARKET_COLLECTOR_ID)
    except ValueError as exc:
        raise DailyRefreshConfigurationError(
            f"MARKET_COLLECTOR_ID is not a valid UUID: "
            f"{settings.MARKET_COLLECTOR_ID!r}"
        ) from exc

    collector = session.get(Collector, collector_id)
    if collector is None:
        raise DailyRefreshConfigurationError(
            f"market collector {collector_id} does not exist in this database"
        )

    expected_provider = settings.MARKET_COLLECTOR_PROVIDER
    if collector.provider != expected_provider:
        raise DailyRefreshConfigurationError(
            f"market collector {collector_id} has provider "
            f"{collector.provider!r}, expected {expected_provider!r}"
        )

    expected_external = settings.MARKET_COLLECTOR_EXTERNAL_ID
    if collector.external_collector_id != expected_external:
        raise DailyRefreshConfigurationError(
            f"market collector {collector_id} points at external collector "
            f"{collector.external_collector_id!r}, expected {expected_external!r}"
        )

    if collector.status is not CollectorStatus.ACTIVE:
        raise DailyRefreshConfigurationError(
            f"market collector {collector_id} is {collector.status.value}, "
            "not active; refusing to refresh a collector that is paused or "
            "retired"
        )

    return collector


def run_daily_refresh(
    session: Session,
    client: BrightDataClient,
    *,
    settings: Settings | None = None,
    day: date | None = None,
    timeout_seconds: float | None = None,
    interval_seconds: float | None = None,
) -> tuple[PipelineRun, bool]:
    """Claim and drive today's market refresh.

    Returns `(run, was_already_claimed)`. The flag distinguishes "this
    invocation started today's work" from "today's work already existed",
    which is what makes a double-fired cron observable rather than merely
    harmless.

    Order matters. Unfinished work is resumed FIRST, before anything new
    is claimed: a collection already paid for at the provider is finished
    before another is started, and if yesterday's run is still in flight
    it is rejoined rather than abandoned. Resume failures are reported
    but never allowed to cost today's refresh.
    """
    settings = settings or get_settings()
    day = day or business_date()
    timeout = _timeout(settings, timeout_seconds)
    interval = _interval(settings, interval_seconds)

    collector = resolve_market_collector(session, settings)
    key = idempotency_key_for(collector.id, day)

    logger.info(
        "daily_refresh_started",
        extra={
            "business_date": day.isoformat(),
            "collector_id": str(collector.id),
            "external_collector_id": collector.external_collector_id,
            "idempotency_key": key,
        },
    )

    _resume_unfinished(
        session,
        client,
        collector_id=collector.id,
        timeout=timeout,
        interval=interval,
    )

    run, already_claimed = start_pipeline_run(
        session, collector=collector, idempotency_key=key
    )

    if already_claimed:
        # Either the cron double-fired, an operator ran it by hand, or
        # today's refresh is genuinely still in flight. All three resolve
        # to the same execution, and none of them triggers a second
        # Bright Data job.
        logger.info(
            "daily_refresh_existing_run",
            extra={
                "business_date": day.isoformat(),
                "collector_id": str(collector.id),
                "pipeline_run_id": str(run.id),
                "pipeline_status": run.status.value,
                "provider_job_id": run.provider_job_id,
            },
        )
        if run.status in TERMINAL_STATUSES:
            # Today is already decided. Nothing to drive, nothing to
            # spend.
            return run, True
        logger.info(
            "daily_refresh_resuming_run",
            extra={
                "pipeline_run_id": str(run.id),
                "pipeline_status": run.status.value,
                "provider_job_id": run.provider_job_id,
            },
        )
    else:
        logger.info(
            "daily_refresh_claimed",
            extra={
                "business_date": day.isoformat(),
                "collector_id": str(collector.id),
                "pipeline_run_id": str(run.id),
                "idempotency_key": key,
            },
        )

    driven = drive_pipeline_run(
        session,
        client,
        pipeline_run_id=run.id,
        timeout_seconds=timeout,
        interval_seconds=interval,
    )
    return driven, already_claimed


def _resume_unfinished(
    session: Session,
    client: BrightDataClient,
    *,
    collector_id: uuid.UUID,
    timeout: float,
    interval: float,
) -> None:
    """Pick up whatever a previous process left in flight FOR THIS COLLECTOR.

    Scoped deliberately. Resuming polls a provider job, and a QUEUED run
    would be triggered outright, so an unscoped recovery pass would let
    this market-only cron advance -- and start -- collections belonging to
    collectors it was never pointed at.

    Reported, never hidden, and never allowed to stop today's work: a
    stuck execution from yesterday must not cost today's refresh.
    """
    try:
        resumed = resume_unfinished_pipeline_runs(
            session,
            client,
            collector_id=collector_id,
            timeout_seconds=timeout,
            interval_seconds=interval,
        )
    except Exception:
        logger.exception("daily_refresh_resume_failed")
        return

    for run in resumed:
        logger.info(
            "daily_refresh_resuming_run",
            extra={
                "pipeline_run_id": str(run.id),
                "collector_id": str(run.collector_id),
                "pipeline_status": run.status.value,
                "provider_job_id": run.provider_job_id,
            },
        )


def _timeout(settings: Settings, override: float | None) -> float:
    if override is not None:
        return override
    configured = settings.DAILY_REFRESH_TIMEOUT_SECONDS
    return DEFAULT_DRIVE_TIMEOUT_SECONDS if configured is None else configured


def _interval(settings: Settings, override: float | None) -> float:
    if override is not None:
        return override
    configured = settings.DAILY_REFRESH_INTERVAL_SECONDS
    return DEFAULT_DRIVE_INTERVAL_SECONDS if configured is None else configured


def exit_code_for(run: PipelineRun) -> int:
    """Translate a persisted verdict into a process exit code.

    The only place the mapping lives, so the module docstring and the
    behaviour cannot drift apart.
    """
    if run.status is PipelineRunStatus.COMPLETED:
        return EXIT_OK
    if run.status is PipelineRunStatus.DEGRADED:
        # RecallGuard detected a problem and recorded it. The job did
        # exactly what it exists to do.
        return EXIT_OK
    if run.status is PipelineRunStatus.FAILED:
        return EXIT_FAILED
    # Still active: the local driver stopped waiting. The execution keeps
    # its provider job id and is resumable.
    return EXIT_TEMPORARY


@contextmanager
def _default_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        # Cron expects a process that exits. Closing the session returns
        # its connection to the pool; disposing the engine actually closes
        # the sockets, so nothing is left holding a connection to the
        # private Railway database while the process winds down.
        session.close()
        get_engine().dispose()


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
) -> int:
    """Entrypoint. Returns the process exit code.

    The session and client factories are injected so the whole entrypoint
    is testable against an in-memory database and a fake provider -- this
    command must never make a real Bright Data call in a test.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.daily_refresh",
        description=(
            "Refresh the production Fix My Itch market dataset once. "
            "Idempotent per Asia/Kolkata business day."
        ),
    )
    parser.add_argument(
        "--business-date",
        type=date.fromisoformat,
        default=None,
        help=(
            "Override the IST business date (YYYY-MM-DD). For backfilling "
            "or replaying a specific day; the cron never passes it."
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.APP_ENV)

    day = args.business_date or business_date()
    started = datetime.now(BUSINESS_TIMEZONE)

    try:
        with session_factory() as session, client_factory() as client:
            run, already_claimed = run_daily_refresh(
                session, client, settings=settings, day=day
            )
            # Read every reported field while the session is open: these
            # are lazy-loaded attributes and the session closes below.
            summary = {
                "pipeline_run_id": str(run.id),
                "collector_id": str(run.collector_id),
                "pipeline_status": run.status.value,
                "provider_job_id": run.provider_job_id,
                "trusted": run.trusted,
                "reliability_state": (
                    run.reliability_state.value if run.reliability_state else None
                ),
                "error": run.error,
            }
            code = exit_code_for(run)
    except DailyRefreshConfigurationError as exc:
        logger.error(
            "daily_refresh_failed",
            extra={"business_date": day.isoformat(), "reason": str(exc)},
        )
        print(f"daily-refresh CONFIGURATION ERROR: {exc}")
        return EXIT_FAILED
    except Exception as exc:
        logger.exception(
            "daily_refresh_failed", extra={"business_date": day.isoformat()}
        )
        print(f"daily-refresh FAILED: {type(exc).__name__}: {exc}")
        return EXIT_FAILED

    duration = (datetime.now(BUSINESS_TIMEZONE) - started).total_seconds()
    logger.info(
        "daily_refresh_finished",
        extra={
            "business_date": day.isoformat(),
            "already_claimed": already_claimed,
            "duration_seconds": round(duration, 2),
            "exit_code": code,
            **summary,
        },
    )

    print(
        f"daily-refresh business_date={day.isoformat()} "
        f"pipeline_run={summary['pipeline_run_id']} "
        f"status={summary['pipeline_status']} "
        f"trusted={summary['trusted']} "
        f"reliability={summary['reliability_state']} "
        f"already_claimed={already_claimed} "
        f"duration={duration:.1f}s exit={code}"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
