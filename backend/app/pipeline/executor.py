"""Asynchronous pipeline execution: claim work, then advance it in steps.

The HTTP request that asks for a refresh must not stay open while Bright
Data scrapes. So the work is split in two:

- `start_pipeline_run` claims the work. It writes one PipelineRun row and
  returns. It never calls the provider, so it is fast and safe to run
  inside a request.
- `resume_pipeline_run` advances one claimed execution by ONE step and
  returns. It never waits for the provider either: if the collection is
  still running it says so and leaves the execution exactly where it was.
- `drive_pipeline_run` repeats that step, sleeping between attempts,
  until the execution is terminal or the caller's local patience runs
  out. This is the only place that waits, and it runs outside the
  request.

Two invariants hold this together.

ONE LOGICAL EXECUTION -> AT MOST ONE ACTIVE PROVIDER JOB.
`start_fix_my_itch_collection` is the only call that asks Bright Data to
start a collection, and it is reached from exactly one place here: the
QUEUED transition. Every other entry into `resume_pipeline_run` already
holds a `provider_job_id` and re-polls that job through
`advance_fix_my_itch_collection`, which cannot trigger. A crash, a
restart, or a local timeout therefore rejoins the existing collection
instead of starting a second one.

LOCAL PATIENCE IS NOT PROVIDER FAILURE.
When `drive_pipeline_run` stops waiting, the execution stays in
WAITING_PROVIDER with its provider job id intact. Nothing is marked
failed, no incident is opened, and RecallGuard is not consulted, because
GapRadar running out of patience is not evidence about Bright Data. The
next resume -- from the daily job, the CLI, or an operator -- picks the
same job back up.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collection.errors import CollectionError
from app.collection.schemas import CollectionRunResult
from app.collection.service import (
    advance_fix_my_itch_collection,
    start_fix_my_itch_collection,
)
from app.db.models import Collector, CollectorRun, PipelineRun
from app.db.models.pipeline_run import ACTIVE_PIPELINE_RUN_STATUSES
from app.domain.enums import PipelineRunStatus, RunStatus
from app.integrations.brightdata.client import BrightDataClient
from app.pipeline.schemas import CollectionFailure, PipelineRunResult
from app.pipeline.service import baseline_from_history, evaluate_and_heal

logger = logging.getLogger(__name__)

# An execution that has not reached a verdict yet. A collector with a row
# in any of these states already has work in flight, so a second request
# joins it rather than starting a competing one.
#
# Taken from the model rather than restated here, because the same tuple
# builds the partial unique index that enforces this in the database. If
# the two ever disagreed, the service and PostgreSQL would hold different
# opinions about what "active" means -- and the one that matters is the
# one the index was built from.
ACTIVE_STATUSES: frozenset[PipelineRunStatus] = frozenset(ACTIVE_PIPELINE_RUN_STATUSES)

TERMINAL_STATUSES: frozenset[PipelineRunStatus] = frozenset(
    {
        PipelineRunStatus.COMPLETED,
        PipelineRunStatus.DEGRADED,
        PipelineRunStatus.FAILED,
    }
)

# Which stage label the collection reports maps to which persisted state.
# The labels are the ones app.collection.errors already uses, so no
# second vocabulary for the same boundaries is introduced.
_STAGE_STATUS: dict[str, PipelineRunStatus] = {
    "collection": PipelineRunStatus.WAITING_PROVIDER,
    "source_validation": PipelineRunStatus.VALIDATING,
    "ingestion": PipelineRunStatus.INGESTING,
}

# How long a single driver keeps stepping one execution forward, and how
# long it waits between steps. GapRadar's own patience, never sent to
# Bright Data: a provider-side deadline once terminated a real production
# run mid-collection. Exceeding it leaves the execution resumable.
DEFAULT_DRIVE_TIMEOUT_SECONDS = 900.0
DEFAULT_DRIVE_INTERVAL_SECONDS = 10.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


# -- claiming work ----------------------------------------------------------


def active_pipeline_run(
    session: Session, *, collector_id: uuid.UUID
) -> PipelineRun | None:
    """This collector's execution that has not reached a verdict, if any."""
    return session.execute(
        select(PipelineRun)
        .where(
            PipelineRun.collector_id == collector_id,
            PipelineRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    ).scalar()


def start_pipeline_run(
    session: Session,
    *,
    collector: Collector,
    idempotency_key: str | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> tuple[PipelineRun, bool]:
    """Claim one pipeline execution for this collector.

    Returns `(run, already_running)`. Makes no provider call at all, so
    it is safe inside an HTTP request and cannot be the thing that takes
    fifteen minutes.

    Deduplication is the point: if this collector already has an active
    execution, that one is returned untouched and `already_running` is
    True. No second Bright Data job is triggered, which is what stops an
    impatient operator (or a double-clicked button) from running two
    collections over the same source at once.

    `idempotency_key` is for a scheduler that wants one logical refresh
    per collector per window. Its uniqueness is enforced by the database,
    so a cron that fires twice resolves to the same execution even if
    both fire at once. A key that already exists returns its execution
    whether or not that execution is still active -- that is what makes a
    completed daily refresh a no-op rather than a second scrape.

    The lookup below is an optimization, not the guarantee. Two
    simultaneous requests can both find nothing and both try to insert;
    the partial unique index `uq_pipeline_runs_active_collector` fails the
    loser, and the loser then reads the winner's row and reports it as
    already running. The race therefore costs one rolled-back INSERT and
    can never produce a second Bright Data job, because no provider call
    happens anywhere in this function.
    """
    claimed = _existing_claim(
        session, collector=collector, idempotency_key=idempotency_key
    )
    if claimed is not None:
        return claimed, True

    run = PipelineRun(
        collector_id=collector.id,
        status=PipelineRunStatus.QUEUED,
        idempotency_key=idempotency_key,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        # Someone else claimed between the lookup and this INSERT. The
        # rollback is required, not cosmetic: the session is unusable for
        # further queries until the failed transaction is discarded, and
        # it also removes the pending row so nothing half-claimed is left.
        session.rollback()

        winner = _existing_claim(
            session, collector=collector, idempotency_key=idempotency_key
        )
        if winner is None:
            # The constraint fired but nothing explains it -- a different
            # violation entirely. Raising beats returning a run that does
            # not correspond to what the caller asked for.
            raise

        logger.info(
            "pipeline_run_claim_lost_race",
            extra={
                "pipeline_run_id": str(winner.id),
                "collector_id": str(collector.id),
                "status": winner.status.value,
            },
        )
        return winner, True

    session.refresh(run)
    logger.info(
        "pipeline_run_queued",
        extra={
            "pipeline_run_id": str(run.id),
            "collector_id": str(collector.id),
        },
    )
    return run, False


def _existing_claim(
    session: Session,
    *,
    collector: Collector,
    idempotency_key: str | None,
) -> PipelineRun | None:
    """The execution this request should join instead of creating one.

    Checked before inserting and again after losing the unique-index
    race, so both paths agree on what "already claimed" means. The key is
    consulted first because it matches terminal executions too: a
    scheduler asking again for a window it already completed must get
    that completed run, not a fresh scrape.
    """
    if idempotency_key is not None:
        existing = session.execute(
            select(PipelineRun).where(PipelineRun.idempotency_key == idempotency_key)
        ).scalar()
        if existing is not None:
            logger.info(
                "pipeline_run_idempotent_hit",
                extra={
                    "pipeline_run_id": str(existing.id),
                    "collector_id": str(collector.id),
                    "idempotency_key": idempotency_key,
                    "status": existing.status.value,
                },
            )
            return existing

    active = active_pipeline_run(session, collector_id=collector.id)
    if active is not None:
        logger.info(
            "pipeline_run_already_active",
            extra={
                "pipeline_run_id": str(active.id),
                "collector_id": str(collector.id),
                "status": active.status.value,
            },
        )
    return active


# -- advancing work ---------------------------------------------------------


def resume_pipeline_run(
    session: Session,
    client: BrightDataClient,
    *,
    pipeline_run_id: uuid.UUID,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> PipelineRun:
    """Advance one execution by a single step. Never waits on the provider.

    The step taken depends only on persisted state, so this is safe to
    call repeatedly and safe to call after a restart:

    - terminal            -> no-op
    - QUEUED              -> trigger the collection, record the provider
                             job id, and leave it WAITING_PROVIDER
    - has a provider job  -> re-poll THAT job. Still running means no
                             change; complete means fetch, validate,
                             ingest, then hand the result to RecallGuard
    - collection finished -> skip straight to RecallGuard

    The one case that cannot be resumed is an execution interrupted
    between claiming the trigger and recording the provider job id: from
    the outside, "the trigger never landed" and "the trigger landed and
    we lost the id" are indistinguishable, and triggering again risks a
    duplicate provider job. That fails closed rather than guessing.
    """
    run = session.get(PipelineRun, pipeline_run_id)
    if run is None:
        raise LookupError(f"pipeline run {pipeline_run_id} not found")

    if run.status in TERMINAL_STATUSES:
        return run

    collector = session.get(Collector, run.collector_id)
    if collector is None:  # pragma: no cover - defensive
        return _fail(session, run, "collector no longer exists", now=now)

    if run.status is PipelineRunStatus.QUEUED:
        return _begin_collection(
            session, client, run=run, collector=collector, now=now, sleep=sleep
        )

    if run.provider_job_id is None or run.collector_run_id is None:
        return _fail(
            session,
            run,
            "interrupted before the provider job was recorded; not retriggering "
            "because a duplicate Bright Data collection cannot be ruled out",
            now=now,
        )

    collector_run = session.get(CollectorRun, run.collector_run_id)
    if collector_run is None:  # pragma: no cover - defensive
        return _fail(session, run, "collector run no longer exists", now=now)

    collection: CollectionRunResult | None = None
    failure: CollectionFailure | None = None

    if collector_run.status in {RunStatus.PENDING, RunStatus.RUNNING}:
        try:
            collection = advance_fix_my_itch_collection(
                session,
                client,
                run=collector_run,
                collector=collector,
                now=now,
                on_stage=lambda label: _advance_to(
                    session, run, _STAGE_STATUS.get(label, run.status)
                ),
            )
        except CollectionError as exc:
            # A real provider or contract failure. The collector run is
            # already finalized FAILED with its evidence; RecallGuard is
            # still asked what it means, exactly as the blocking pipeline
            # does, because evaluating failed runs is the point.
            failure = CollectionFailure(
                stage=exc.stage,
                error=type(exc).__name__,
                message=str(exc),
                collector_run_id=exc.collector_run_id,
            )
        else:
            if collection is None:
                # Still scraping. Not a failure, not a timeout, and not
                # RecallGuard's business yet.
                return _advance_to(session, run, PipelineRunStatus.WAITING_PROVIDER)

    return _finish(
        session,
        client,
        run=run,
        collector=collector,
        collection=collection,
        failure=failure,
        collector_run=collector_run,
        now=now,
        sleep=sleep,
    )


def drive_pipeline_run(
    session: Session,
    client: BrightDataClient,
    *,
    pipeline_run_id: uuid.UUID,
    timeout_seconds: float = DEFAULT_DRIVE_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_DRIVE_INTERVAL_SECONDS,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> PipelineRun:
    """Step one execution forward until it is terminal, or until we stop waiting.

    The waiting happens here and nowhere else. Running out of the local
    budget is NOT a failure: the execution is left exactly as it is --
    WAITING_PROVIDER, provider job id intact, no incident, no error --
    so the next caller resumes the same provider job. That is the whole
    difference between GapRadar being impatient and Bright Data being
    broken.
    """
    deadline = now().timestamp() + timeout_seconds

    while True:
        run = resume_pipeline_run(
            session, client, pipeline_run_id=pipeline_run_id, now=now, sleep=sleep
        )
        if run.status in TERMINAL_STATUSES:
            return run

        if now().timestamp() >= deadline:
            logger.info(
                "pipeline_run_local_wait_exhausted",
                extra={
                    "pipeline_run_id": str(run.id),
                    "collector_id": str(run.collector_id),
                    "status": run.status.value,
                    "provider_job_id": run.provider_job_id,
                    "timeout_seconds": timeout_seconds,
                },
            )
            return run

        sleep(interval_seconds)


def resume_unfinished_pipeline_runs(
    session: Session,
    client: BrightDataClient,
    *,
    timeout_seconds: float = DEFAULT_DRIVE_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_DRIVE_INTERVAL_SECONDS,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> list[PipelineRun]:
    """Pick up every execution a previous process left in flight.

    This is what makes the local executor's lack of durability
    survivable: the in-memory task is lost when the process exits, but
    the PipelineRun row and its provider job id are not, so a later
    scheduled invocation rejoins the same Bright Data collection instead
    of abandoning it or starting another.
    """
    pending = list(
        session.execute(
            select(PipelineRun)
            .where(PipelineRun.status.in_(ACTIVE_STATUSES))
            .order_by(PipelineRun.created_at)
        ).scalars()
    )
    if pending:
        logger.info(
            "pipeline_runs_resuming", extra={"pipeline_run_count": len(pending)}
        )

    return [
        drive_pipeline_run(
            session,
            client,
            pipeline_run_id=run.id,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            now=now,
            sleep=sleep,
        )
        for run in pending
    ]


# -- steps ------------------------------------------------------------------


def _begin_collection(
    session: Session,
    client: BrightDataClient,
    *,
    run: PipelineRun,
    collector: Collector,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> PipelineRun:
    """Trigger the collection. The only path in this module that can.

    COLLECTING is committed before the trigger request goes out, so a
    process that dies mid-trigger leaves a row that resume refuses to
    retrigger rather than one that looks untouched and gets triggered
    twice.
    """
    run.started_at = run.started_at or now()
    _advance_to(session, run, PipelineRunStatus.COLLECTING)

    try:
        collector_run = start_fix_my_itch_collection(
            session, client, collector=collector, now=now
        )
    except CollectionError as exc:
        # A trigger failure yields no collection id and therefore no
        # CollectorRun to evaluate. RecallGuard is still told, and still
        # invents neither a run nor an incident.
        failure = CollectionFailure(
            stage=exc.stage,
            error=type(exc).__name__,
            message=str(exc),
            collector_run_id=exc.collector_run_id,
        )
        return _finish(
            session,
            client,
            run=run,
            collector=collector,
            collection=None,
            failure=failure,
            collector_run=None,
            now=now,
            sleep=sleep,
        )

    run.provider_job_id = collector_run.external_run_id
    run.collector_run_id = collector_run.id
    logger.info(
        "pipeline_run_collection_started",
        extra={
            "pipeline_run_id": str(run.id),
            "collector_id": str(collector.id),
            "collector_run_id": str(collector_run.id),
            "provider_job_id": collector_run.external_run_id,
        },
    )
    return _advance_to(session, run, PipelineRunStatus.WAITING_PROVIDER)


def _finish(
    session: Session,
    client: BrightDataClient,
    *,
    run: PipelineRun,
    collector: Collector,
    collection: CollectionRunResult | None,
    failure: CollectionFailure | None,
    collector_run: CollectorRun | None,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> PipelineRun:
    """Hand the finished collection to RecallGuard and record the verdict.

    The verdict itself is computed by app.pipeline.service.evaluate_and_heal
    -- the same function the blocking pipeline uses -- so an asynchronous
    execution and a synchronous one cannot reach different conclusions
    about the same data.
    """
    _advance_to(session, run, PipelineRunStatus.VERIFYING)

    baseline = baseline_from_history(
        session,
        collector_id=collector.id,
        exclude_run_id=collector_run.id if collector_run is not None else None,
    )

    try:
        result = evaluate_and_heal(
            session,
            client,
            collector=collector,
            collection=collection,
            failure=failure,
            collector_run_id=collector_run.id if collector_run is not None else None,
            baseline=baseline,
            now=now,
            sleep=sleep,
        )
    except Exception as exc:
        # RecallGuard itself failing is an operational fault, not a trust
        # verdict, so it is recorded as FAILED and never as DEGRADED.
        logger.exception(
            "pipeline_run_verification_failed",
            extra={"pipeline_run_id": str(run.id)},
        )
        return _fail(session, run, f"{type(exc).__name__}: {exc}", now=now)

    return _record_result(session, run, result, now=now)


def _record_result(
    session: Session,
    run: PipelineRun,
    result: PipelineRunResult,
    *,
    now: Callable[[], datetime],
) -> PipelineRun:
    """Copy RecallGuard's verdict onto the execution and close it out."""
    run.trusted = result.trusted
    run.reliability_state = result.reliability_state
    run.incident_id = result.incident_id
    # The run whose data this execution stands behind: the verification
    # run when a repair was proven, otherwise the one it collected.
    run.collector_run_id = (
        result.trusted_collector_run_id
        or result.collector_run_id
        or run.collector_run_id
    )
    run.completed_at = now()
    run.error = None

    status = (
        PipelineRunStatus.COMPLETED if result.trusted else PipelineRunStatus.DEGRADED
    )
    logger.info(
        "pipeline_run_finished",
        extra={
            "pipeline_run_id": str(run.id),
            "collector_id": str(run.collector_id),
            "status": status.value,
            "outcome": result.outcome.value,
            "reliability_state": result.reliability_state.value,
            "trusted": result.trusted,
        },
    )
    return _advance_to(session, run, status)


def _fail(
    session: Session,
    run: PipelineRun,
    message: str,
    *,
    now: Callable[[], datetime],
) -> PipelineRun:
    """Close the execution as FAILED: could not be carried out.

    Distinct from DEGRADED, which is a completed execution that
    RecallGuard judged untrustworthy. `trusted` is left NULL rather than
    set false, because an execution that never finished produced no
    verdict about the data -- and previously trusted data is untouched
    either way.
    """
    run.error = message
    run.completed_at = now()
    logger.warning(
        "pipeline_run_failed",
        extra={
            "pipeline_run_id": str(run.id),
            "collector_id": str(run.collector_id),
            "reason": message,
        },
    )
    return _advance_to(session, run, PipelineRunStatus.FAILED)


def _advance_to(
    session: Session, run: PipelineRun, status: PipelineRunStatus
) -> PipelineRun:
    """Persist a lifecycle transition immediately.

    Committed as it happens rather than at the end, so a poller watching
    the status endpoint sees the execution move, and so a process that
    dies mid-flight leaves behind the last state it actually reached.
    """
    if run.status is not status:
        run.status = status
    session.commit()
    session.refresh(run)
    return run
