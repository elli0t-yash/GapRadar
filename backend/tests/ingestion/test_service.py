from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CollectorRun, Signal, Source
from app.domain.enums import SignalType
from app.ingestion.schemas import RejectionReason
from app.ingestion.service import ingest_collector_output
from tests.ingestion.factories import make_raw_record


def test_valid_record_is_normalized_and_persisted(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[make_raw_record()],
    )

    assert result.accepted == 1
    assert result.duplicates == 0
    assert result.rejected == []
    assert len(result.persisted_signal_ids) == 1

    signal = db_session.get(Signal, result.persisted_signal_ids[0])
    assert signal is not None
    assert signal.external_id == "post-1"
    assert signal.canonical_url == "https://reddit.com/r/startups/post-1"
    assert signal.signal_type is SignalType.COMPLAINT
    assert signal.source_id == source.id
    assert signal.collector_run_id == collector_run.id


def test_blank_record_rejected_with_explicit_reason(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[make_raw_record(title="   ", body="   ")],
    )

    assert result.accepted == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is RejectionReason.MISSING_REQUIRED_FIELD
    assert result.rejected[0].index == 0


def test_invalid_url_rejected(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[make_raw_record(canonical_url="not-a-url")],
    )

    assert result.accepted == 0
    assert result.rejected[0].reason is RejectionReason.INVALID_URL


def test_invalid_timestamp_rejected(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[make_raw_record(observed_at="not-a-timestamp")],
    )

    assert result.accepted == 0
    assert result.rejected[0].reason is RejectionReason.INVALID_TIMESTAMP


def test_metadata_preserved_but_inert(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[make_raw_record(metadata={"upvotes": 42, "__class__": "ignored"})],
    )

    signal = db_session.get(Signal, result.persisted_signal_ids[0])
    assert signal is not None
    assert signal.signal_metadata == {"upvotes": 42, "__class__": "ignored"}


def test_duplicate_within_same_payload_counted_once(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    record = make_raw_record()
    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[record, dict(record)],
    )

    assert result.accepted == 1
    assert result.duplicates == 1
    assert db_session.query(Signal).count() == 1


def test_duplicate_across_ingestion_calls_no_duplicate_row(
    db_session: Session,
    source: Source,
    collector_run: CollectorRun,
    other_collector_run: CollectorRun,
) -> None:
    record = make_raw_record()

    first = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[record],
    )
    second = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=other_collector_run.id,
        records=[dict(record)],
    )

    assert first.accepted == 1
    assert second.accepted == 0
    assert second.duplicates == 1
    assert db_session.query(Signal).count() == 1


def test_provenance_collector_run_id_is_first_seen_not_overwritten(
    db_session: Session,
    source: Source,
    collector_run: CollectorRun,
    other_collector_run: CollectorRun,
) -> None:
    record = make_raw_record()
    ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[record],
    )
    ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=other_collector_run.id,
        records=[dict(record)],
    )

    signal = db_session.execute(
        select(Signal).where(
            Signal.source_id == source.id, Signal.external_id == "post-1"
        )
    ).scalar_one()
    # collector_run_id still points at the run that first persisted this
    # logical signal, not the later run that re-observed it.
    assert signal.collector_run_id == collector_run.id


def test_fallback_identity_used_when_external_id_absent_and_deduplicates(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    record = make_raw_record()
    del record["external_id"]

    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[record, dict(record)],
    )

    assert result.accepted == 1
    assert result.duplicates == 1
    signal = db_session.get(Signal, result.persisted_signal_ids[0])
    assert signal is not None
    assert signal.external_id.startswith("fp:")


def test_malformed_record_does_not_corrupt_session_for_later_valid_records(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    bad = make_raw_record(canonical_url="not-a-url")
    bad["external_id"] = "bad-1"
    good = make_raw_record()
    good["external_id"] = "good-1"

    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[bad, good],
    )

    assert result.accepted == 1
    assert len(result.rejected) == 1
    assert db_session.query(Signal).count() == 1
    # the session must still be usable after the failed record
    assert db_session.query(Source).count() == 1


def test_duplicate_insert_race_does_not_corrupt_session_for_later_records(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    # Simulate a duplicate that the in-batch/pre-check SELECT could not
    # catch (e.g. already committed by a concurrent writer) by inserting
    # directly, bypassing the service's own dedup logic, then ingesting
    # the same identity plus one genuinely new record in a single call.
    existing = Signal(
        source_id=source.id,
        collector_run_id=collector_run.id,
        external_id="already-there",
        canonical_url="https://reddit.com/r/startups/already-there",
        title="Existing",
        body="Existing body",
        signal_type=SignalType.COMPLAINT,
        observed_at=datetime.fromisoformat(make_raw_record()["observed_at"]),
    )
    db_session.add(existing)
    db_session.commit()

    dup = make_raw_record(external_id="already-there")
    good = make_raw_record(external_id="brand-new")

    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[dup, good],
    )

    assert result.accepted == 1
    assert result.duplicates == 1
    assert db_session.query(Signal).count() == 2


def test_ingest_does_not_promote_trust_or_call_downstream_systems(
    db_session: Session, source: Source, collector_run: CollectorRun
) -> None:
    """Architectural trust-boundary assertion: ingestion ends at Signal
    persistence. It has no import of, or dependency on, RecallGuard,
    Harness, the Opportunity Engine, or BrightDataClient, and the Signal
    model itself has no "trusted" field for ingestion to set.
    """
    import ast
    import inspect

    import app.ingestion.service as service_module

    source_code = inspect.getsource(service_module)
    tree = ast.parse(source_code)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "recallguard",
        "harness",
        "opportunity_engine",
        "brightdata",
    ):
        assert not any(forbidden in name.lower() for name in imported_names)

    result = ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[make_raw_record()],
    )
    signal = db_session.get(Signal, result.persisted_signal_ids[0])
    assert signal is not None
    assert not hasattr(signal, "trusted")
    assert not hasattr(signal, "is_trusted")
