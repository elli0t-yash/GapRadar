"""Orchestrates ONE Fix My Itch collection run, end to end.

    trigger -> track -> poll -> fetch -> source validation -> ingest -> finalize

Scope boundaries this module deliberately respects:

- It owns COLLECTION EXECUTION only: did Bright Data run the collector,
  and did the resulting dataset satisfy the source's own contract? It
  records no trust, health, confidence, or recovery verdict -- those are
  RecallGuard's, and RecallGuard does not exist yet. A Bright Data
  collection that finishes is never described here as "healthy",
  "trusted", or "recovered".
- It never writes Signal rows, computes identities, or deduplicates.
  app.ingestion owns all of that and stays authoritative; this module
  only decides where that work's transaction begins and ends.
- It never repairs source data. app.integrations.brightdata.fix_my_itch
  decides what is valid, and one invalid record fails the whole batch.
- It never talks HTTP itself. BrightDataClient is the only transport.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collection.errors import (
    CollectionError,
    CollectionExecutionError,
    CollectionIngestionError,
    CollectionTimeoutError,
    CollectionTriggerError,
    MalformedCollectionPayloadError,
    SourceContractValidationError,
)
from app.collection.schemas import (
    DEFAULT_POLLING_POLICY,
    CollectionRunResult,
    PollingPolicy,
)
from app.db.models import Collector, CollectorRun
from app.domain.enums import RunStatus
from app.ingestion.schemas import IngestionResult
from app.ingestion.service import ingest_collector_output
from app.integrations.brightdata.client import BrightDataClient
from app.integrations.brightdata.errors import (
    BrightDataError,
    BrightDataInvalidResponseError,
)
from app.integrations.brightdata.fix_my_itch import (
    FIX_MY_ITCH_SOURCE_URL,
    FixMyItchDatasetReport,
    to_raw_provider_record,
    validate_dataset,
)
from app.integrations.brightdata.schemas import CollectorExecution, CollectorRunStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run_fix_my_itch_collection(
    session: Session,
    client: BrightDataClient,
    *,
    collector: Collector,
    polling: PollingPolicy = DEFAULT_POLLING_POLICY,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectionRunResult:
    """Execute one Fix My Itch collection run.

    Dependencies are explicit rather than global: the Session and the
    BrightDataClient are supplied by the caller, the Bright Data
    collector id comes from the persisted Collector row (never from a
    literal in business logic), and `now`/`sleep` are injected so the
    polling loop is testable without real time passing.

    Raises a CollectionError subclass on any failure, always chained
    (`raise ... from`) to the original cause.
    """
    started_at = now()
    execution = _trigger(client, collector)
    run = _open_run(session, collector, execution, started_at=started_at)

    try:
        _poll_until_complete(client, run, polling=polling, now=now, sleep=sleep)
        records = _fetch_dataset(client, run)
        report = _validate(run, records, source_id=collector.source_id)
        ingestion = _ingest(
            session, run, report, source_id=collector.source_id, observed_at=now()
        )
    except CollectionError as exc:
        _finalize_failure(session, run, exc, completed_at=now())
        raise
    except Exception:
        # Anything unforeseen still closes the run out rather than
        # leaving it stuck in RUNNING; the original exception is
        # re-raised untouched so the cause is never swallowed.
        failure = CollectionError(
            f"Unexpected failure during collection run {run.external_run_id!r}",
            collector_run_id=run.id,
        )
        _finalize_failure(session, run, failure, completed_at=now())
        raise

    return _finalize_success(
        session,
        run,
        report=report,
        ingestion=ingestion,
        fetched_record_count=len(records),
        completed_at=now(),
    )


# -- stages -----------------------------------------------------------------


def _trigger(client: BrightDataClient, collector: Collector) -> CollectorExecution:
    """Trigger the production collector.

    The request carries only the collector id, queue_next=1, and the
    input array -- see BrightDataClient.trigger_collector_run. It must
    never carry `version=dev` (that runs an unpublished draft) or
    `deadline` (which once terminated a real production run
    mid-collection); neither is expressible through the client, and the
    client's own tests hold that line.
    """
    try:
        execution = client.trigger_collector_run(
            collector.external_collector_id, [{"url": FIX_MY_ITCH_SOURCE_URL}]
        )
    except BrightDataError as exc:
        # No CollectorRun row is created here: external_run_id is NOT
        # NULL and Bright Data only issues the collection id on a
        # successful trigger. Inventing a placeholder id would put a
        # fabricated identifier into the run history, so the failure is
        # raised instead. See the summary's schema-gap note.
        raise CollectionTriggerError(
            f"Bright Data trigger failed for collector "
            f"{collector.external_collector_id!r}"
        ) from exc

    logger.info(
        "collector_triggered",
        extra={
            "collector_id": str(collector.id),
            "external_collector_id": collector.external_collector_id,
            "external_run_id": execution.external_run_id,
        },
    )
    return execution


def _open_run(
    session: Session,
    collector: Collector,
    execution: CollectorExecution,
    *,
    started_at: datetime,
) -> CollectorRun:
    """Persist the run as RUNNING before any polling begins.

    Committed immediately so an in-flight collection is visible to other
    sessions (and to a later operator) rather than existing only in this
    process's memory.
    """
    run = CollectorRun(
        collector_id=collector.id,
        external_run_id=execution.external_run_id,
        status=RunStatus.RUNNING,
        started_at=started_at,
        record_count=0,
        raw_metadata={"provider": execution.provider_metadata},
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _poll_until_complete(
    client: BrightDataClient,
    run: CollectorRun,
    *,
    polling: PollingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> CollectorExecution:
    """Poll Bright Data until the collection is complete.

    The only polling loop in the codebase: interval and local timeout
    come from PollingPolicy, the wait happens through the injected
    sleeper (never a bare loop), and the budget is enforced here in
    GapRadar -- never handed to Bright Data as a `deadline`.
    """
    deadline = now() + timedelta(seconds=polling.timeout_seconds)
    polls = 0

    while True:
        polls += 1
        try:
            execution = client.get_collector_run_status(run.external_run_id)
        except BrightDataInvalidResponseError as exc:
            raise MalformedCollectionPayloadError(
                f"Bright Data returned an unreadable status payload for "
                f"collection {run.external_run_id!r}",
                collector_run_id=run.id,
            ) from exc
        except BrightDataError as exc:
            raise CollectionExecutionError(
                f"Bright Data failed while collection {run.external_run_id!r} "
                "was running",
                collector_run_id=run.id,
            ) from exc

        if execution.status is CollectorRunStatus.SUCCEEDED:
            logger.info(
                "collection_completed",
                extra={
                    "collector_run_id": str(run.id),
                    "external_run_id": run.external_run_id,
                    "polls": polls,
                    "provider_record_count": execution.record_count,
                },
            )
            return execution

        logger.info(
            "collection_polling",
            extra={
                "collector_run_id": str(run.id),
                "external_run_id": run.external_run_id,
                "provider_status": execution.status.value,
                "polls": polls,
            },
        )

        if now() >= deadline:
            raise CollectionTimeoutError(
                f"Local orchestration budget of {polling.timeout_seconds}s "
                f"elapsed while waiting for collection {run.external_run_id!r}",
                collector_run_id=run.id,
                timeout_seconds=polling.timeout_seconds,
                polls=polls,
            )
        sleep(polling.interval_seconds)


def _fetch_dataset(client: BrightDataClient, run: CollectorRun) -> list[dict[str, Any]]:
    """Retrieve the completed dataset.

    A malformed response is never degraded into an empty or shortened
    dataset: the client itself rejects a payload that is not an array of
    JSON objects (BrightDataMalformedDatasetError names the offending
    row), and that arrives here as a payload failure. A genuinely empty
    dataset is passed through as zero records -- "the scraper produced
    nothing" is a real execution outcome for a later phase to judge, not
    a parse failure.
    """
    try:
        output = client.get_collector_output(run.external_run_id)
    except BrightDataInvalidResponseError as exc:
        raise MalformedCollectionPayloadError(
            f"Bright Data dataset for collection {run.external_run_id!r} was "
            f"not a usable array of record objects: {exc}",
            collector_run_id=run.id,
        ) from exc
    except BrightDataError as exc:
        raise CollectionExecutionError(
            f"Bright Data failed while serving the dataset for collection "
            f"{run.external_run_id!r}",
            collector_run_id=run.id,
        ) from exc

    logger.info(
        "dataset_fetched",
        extra={
            "collector_run_id": str(run.id),
            "external_run_id": run.external_run_id,
            "fetched_record_count": len(output.records),
        },
    )
    return output.records


def _validate(
    run: CollectorRun, records: list[dict[str, Any]], *, source_id: uuid.UUID
) -> FixMyItchDatasetReport:
    """Apply the source contract to every record -- fail-closed.

    A single invalid record blocks the entire dataset: nothing is
    ingested, and the full invalid-record report is preserved on the run
    as evidence for RecallGuard later. Nothing is normalized or repaired
    here (a tam_score of 60 fails; it never becomes 6).

    Duplicates are reported, not fatal: the source repeating itself is
    not a contract violation, and the ingestion pipeline's own
    deterministic dedup stays authoritative.
    """
    report = validate_dataset(records, source_id=source_id)
    logger.info(
        "source_validation_summary",
        extra={
            "collector_run_id": str(run.id),
            "external_run_id": run.external_run_id,
            "fetched_record_count": len(records),
            "valid_record_count": len(report.valid),
            "invalid_record_count": len(report.invalid),
            "source_duplicate_count": len(report.duplicates),
        },
    )
    if report.invalid:
        first = report.invalid[0]
        raise SourceContractValidationError(
            f"{len(report.invalid)} of {len(records)} Fix My Itch records "
            f"violated the source contract (first: index {first.index}, "
            f"{first.reason.value})",
            collector_run_id=run.id,
            report=report,
            fetched_record_count=len(records),
        )
    return report


def _ingest(
    session: Session,
    run: CollectorRun,
    report: FixMyItchDatasetReport,
    *,
    source_id: uuid.UUID,
    observed_at: datetime,
) -> IngestionResult:
    """Hand the validated dataset to the existing ingestion pipeline.

    The orchestrator maps records through the source adapter and stops
    there: identity, deduplication, and Signal persistence all stay in
    app.ingestion. `observed_at` is supplied because Fix My Itch
    publishes no per-row timestamp; it never affects signal identity, so
    re-running a collection stays idempotent.

    commit=False hands the transaction boundary to this orchestrator:
    the signals are flushed but stay uncommitted until the run is
    finalized SUCCEEDED in the same transaction, so a failed run can
    never leave newly persisted signals behind.
    """
    try:
        ingestion = ingest_collector_output(
            session,
            source_id=source_id,
            collector_run_id=run.id,
            records=[
                to_raw_provider_record(valid.record, observed_at=observed_at)
                for valid in report.valid
            ],
            commit=False,
        )
    except SQLAlchemyError as exc:
        raise CollectionIngestionError(
            f"Persisting signals for collection {run.external_run_id!r} failed",
            collector_run_id=run.id,
        ) from exc

    if ingestion.rejected:
        # Source validation already passed, so a generic-contract
        # rejection means the adapter's mapping and the ingestion
        # contract have drifted. The run must not claim success.
        raise CollectionIngestionError(
            f"{len(ingestion.rejected)} validated Fix My Itch records were "
            "rejected by the ingestion pipeline",
            collector_run_id=run.id,
            rejected=[
                rejected.model_dump(mode="json") for rejected in ingestion.rejected
            ],
        )

    logger.info(
        "ingestion_summary",
        extra={
            "collector_run_id": str(run.id),
            "external_run_id": run.external_run_id,
            "accepted": ingestion.accepted,
            "duplicates": ingestion.duplicates,
        },
    )
    return ingestion


# -- run finalization -------------------------------------------------------


def _finalize_success(
    session: Session,
    run: CollectorRun,
    *,
    report: FixMyItchDatasetReport,
    ingestion: IngestionResult,
    fetched_record_count: int,
    completed_at: datetime,
) -> CollectionRunResult:
    """Mark the run SUCCEEDED and commit it together with the signals.

    This is the single commit of the success path: the ingested signals
    are still pending in this transaction, so they and the terminal
    SUCCEEDED status become durable in the same atomic unit. There is no
    window in which one exists without the other.

    SUCCEEDED describes execution, not trustworthiness: no health,
    confidence, or recovery value is written anywhere here.
    """
    run.status = RunStatus.SUCCEEDED
    run.completed_at = completed_at
    run.record_count = fetched_record_count
    run.raw_metadata = {
        **(run.raw_metadata or {}),
        "orchestration": {
            "stage": "completed",
            "fetched_record_count": fetched_record_count,
            "valid_record_count": len(report.valid),
            "invalid_record_count": 0,
            "source_duplicate_count": len(report.duplicates),
            "ingestion": {
                "accepted": ingestion.accepted,
                "duplicates": ingestion.duplicates,
            },
        },
    }
    session.commit()

    logger.info(
        "collection_run_finished",
        extra={
            "collector_run_id": str(run.id),
            "external_run_id": run.external_run_id,
            "status": run.status.value,
        },
    )
    return CollectionRunResult(
        collector_run_id=run.id,
        external_run_id=run.external_run_id,
        status=run.status,
        fetched_record_count=fetched_record_count,
        valid_record_count=len(report.valid),
        invalid_record_count=0,
        source_duplicate_count=len(report.duplicates),
        accepted=ingestion.accepted,
        duplicates=ingestion.duplicates,
        persisted_signal_ids=ingestion.persisted_signal_ids,
    )


def _finalize_failure(
    session: Session,
    run: CollectorRun,
    exc: CollectionError,
    *,
    completed_at: datetime,
) -> None:
    """Mark the run FAILED and keep the failure evidence on the row.

    FAILED means "this orchestration run produced no ingested signals",
    not "this source is untrustworthy" -- `stage` records which boundary
    gave way so a later phase can tell a provider outage apart from a
    source-contract violation.

    The session is rolled back first, which is what makes a failed run
    leave no trace in the signals table: ingestion runs with
    commit=False, so any rows it flushed for this run are still pending
    and the rollback discards every one of them. Signals ingested by
    earlier runs were committed by those runs and are untouched. The run
    row itself was committed before polling began, so it survives the
    rollback and can be finalized in a clean transaction.
    """
    session.rollback()
    run.status = RunStatus.FAILED
    run.completed_at = completed_at
    run.raw_metadata = {
        **(run.raw_metadata or {}),
        "orchestration": {
            "stage": exc.stage,
            "error": type(exc).__name__,
            "message": str(exc),
            **exc.evidence(),
        },
    }
    session.commit()

    logger.warning(
        "collection_run_failed",
        extra={
            "collector_run_id": str(run.id),
            "external_run_id": run.external_run_id,
            "stage": exc.stage,
            "error": type(exc).__name__,
        },
    )
