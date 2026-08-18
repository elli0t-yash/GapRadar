import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Signal
from app.ingestion.normalizer import RecordRejectedError, normalize_record
from app.ingestion.schemas import (
    IngestionResult,
    RawProviderRecord,
    RejectedRecord,
)


def ingest_collector_output(
    session: Session,
    *,
    source_id: uuid.UUID,
    collector_run_id: uuid.UUID,
    records: list[RawProviderRecord],
    commit: bool = True,
) -> IngestionResult:
    """Validate, normalize, deduplicate, and persist Bright Data collector
    output as Signal rows.

    This function ends at canonical Signal persistence. It never marks a
    Signal trusted, never touches Source/Collector/CollectorRun state
    beyond reading collector_run_id as a foreign key, and never calls
    RecallGuard, Harness, the Opportunity Engine, or BrightDataClient.
    Trust decisions belong to a later phase.

    Transaction strategy: the caller-supplied Session is used as-is (no
    new engine, no independent commits). Each candidate insert is
    attempted inside its own SAVEPOINT (Session.begin_nested()) so that a
    unique-constraint collision on one record -- whether a genuine
    concurrent writer or a duplicate this function's own pre-check missed
    -- raises and is caught for that record alone, without poisoning the
    outer transaction or forcing already-accepted records in this same
    call to be rolled back.

    `commit` controls the outer transaction boundary only:

    - commit=True (default, and every historical caller's behavior):
      exactly one outer commit happens at the end, covering the batch.
    - commit=False: the accepted rows are flushed but left uncommitted,
      so the caller can extend the transaction across further work and
      commit -- or roll back -- the whole thing as one unit. A caller
      that passes commit=False owns the transaction and MUST commit or
      roll back; nothing is durable until it does.

    Deduplication is unaffected either way: the in-batch check, the
    SELECT pre-check, and the database's unique constraint all operate
    within the session's transaction and see this batch's own pending
    inserts after each flush.
    """
    accepted = 0
    duplicates = 0
    rejected: list[RejectedRecord] = []
    persisted_signal_ids: list[uuid.UUID] = []
    seen_external_ids_in_batch: set[str] = set()

    for index, raw in enumerate(records):
        try:
            normalized = normalize_record(raw, source_id=source_id)
        except RecordRejectedError as exc:
            rejected.append(
                RejectedRecord(
                    index=index, reason=exc.reason, detail=exc.detail, raw=raw
                )
            )
            continue

        if normalized.external_id in seen_external_ids_in_batch:
            duplicates += 1
            continue

        # Fast-path check: skip the SAVEPOINT/flush entirely for the
        # common case of re-ingesting an already-known signal. This is
        # NOT the correctness mechanism (see the IntegrityError handling
        # below) -- it is purely an optimization, since SELECT-then-INSERT
        # alone is race-prone.
        already_exists = (
            session.execute(
                select(Signal.id).where(
                    Signal.source_id == source_id,
                    Signal.external_id == normalized.external_id,
                )
            ).scalar_one_or_none()
            is not None
        )
        if already_exists:
            seen_external_ids_in_batch.add(normalized.external_id)
            duplicates += 1
            continue

        signal = Signal(
            source_id=source_id,
            collector_run_id=collector_run_id,
            external_id=normalized.external_id,
            canonical_url=normalized.canonical_url,
            title=normalized.title,
            body=normalized.body,
            signal_type=normalized.signal_type,
            signal_metadata=normalized.metadata,
            observed_at=normalized.observed_at,
        )
        try:
            with session.begin_nested():
                session.add(signal)
                session.flush()
        except IntegrityError:
            # The database's unique constraint on (source_id, external_id)
            # is the final authority: this catches the race the SELECT
            # above cannot close (a concurrent writer, or a duplicate
            # already committed by an earlier statement in this same
            # call). The SAVEPOINT rollback undoes only this record.
            seen_external_ids_in_batch.add(normalized.external_id)
            duplicates += 1
            continue

        seen_external_ids_in_batch.add(normalized.external_id)
        accepted += 1
        persisted_signal_ids.append(signal.id)

    if commit:
        session.commit()

    return IngestionResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        persisted_signal_ids=persisted_signal_ids,
    )
