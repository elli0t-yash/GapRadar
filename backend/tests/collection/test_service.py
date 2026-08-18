import json
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collection.errors import (
    CollectionExecutionError,
    CollectionIngestionError,
    CollectionTimeoutError,
    CollectionTriggerError,
    MalformedCollectionPayloadError,
    SourceContractValidationError,
)
from app.collection.schemas import CollectionRunResult, PollingPolicy
from app.collection.service import run_fix_my_itch_collection
from app.config import Settings
from app.db.models import Collector, CollectorRun, Signal
from app.domain.enums import RunStatus, SignalType
from app.integrations.brightdata.errors import BrightDataMalformedDatasetError
from tests.collection.conftest import (
    COLLECTION_ID,
    FakeClock,
    ScriptedBrightData,
    building_then,
)
from tests.integrations.brightdata.conftest import make_client

FAST_POLLING = PollingPolicy(interval_seconds=1.0, timeout_seconds=60.0)


class InjectedDatabaseError(SQLAlchemyError):
    """Stand-in for a database failure raised mid-ingestion."""


def run(
    db_session: Session,
    collector: Collector,
    handler: ScriptedBrightData,
    *,
    polling: PollingPolicy = FAST_POLLING,
    clock: FakeClock | None = None,
    settings: Settings | None = None,
) -> CollectionRunResult:
    clock = clock or FakeClock()
    with make_client(settings, handler) as client:
        return run_fix_my_itch_collection(
            db_session,
            client,
            collector=collector,
            polling=polling,
            now=clock.now,
            sleep=clock.sleep,
        )


def handler_for(
    dataset: list[dict[str, Any]], *, building_polls: int = 0
) -> ScriptedBrightData:
    return ScriptedBrightData(
        get_responses=building_then(dataset, building_polls=building_polls)
    )


def signal_count(db_session: Session) -> int:
    return db_session.execute(select(func.count()).select_from(Signal)).scalar_one()


def only_run(db_session: Session) -> CollectorRun:
    runs = list(db_session.execute(select(CollectorRun)).scalars())
    assert len(runs) == 1
    return runs[0]


# --- happy path ------------------------------------------------------------


def test_happy_path_runs_collection_end_to_end(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    handler = ScriptedBrightData(get_responses=building_then(dataset, building_polls=1))

    result = run(db_session, collector, handler, settings=brightdata_settings)

    assert result.status is RunStatus.SUCCEEDED
    assert result.external_run_id == COLLECTION_ID
    assert result.fetched_record_count == len(dataset)
    assert result.valid_record_count == len(dataset)
    assert result.invalid_record_count == 0
    assert result.source_duplicate_count == 0
    assert result.accepted == len(dataset)
    assert result.duplicates == 0
    assert len(result.persisted_signal_ids) == len(dataset)
    assert signal_count(db_session) == len(dataset)


def test_happy_path_finalizes_the_collector_run(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    clock = FakeClock()

    result = run(
        db_session,
        collector,
        handler_for(dataset),
        clock=clock,
        settings=brightdata_settings,
    )

    run_row = db_session.get(CollectorRun, result.collector_run_id)
    assert run_row is not None
    assert run_row.collector_id == collector.id
    assert run_row.external_run_id == COLLECTION_ID
    assert run_row.status is RunStatus.SUCCEEDED
    assert run_row.started_at is not None
    assert run_row.completed_at is not None
    assert run_row.record_count == len(dataset)
    assert run_row.raw_metadata["provider"] == {"collection_id": COLLECTION_ID}
    assert run_row.raw_metadata["orchestration"]["stage"] == "completed"


def test_happy_path_persists_problem_signals_with_score_metadata(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    result = run(
        db_session, collector, handler_for(dataset), settings=brightdata_settings
    )

    signal = db_session.get(Signal, result.persisted_signal_ids[0])
    assert signal is not None
    assert signal.signal_type is SignalType.PROBLEM
    assert signal.collector_run_id == result.collector_run_id
    assert signal.source_id == collector.source_id
    assert signal.signal_metadata["tam_score"] == dataset[0]["tam_score"]
    assert signal.signal_metadata["industry"] == dataset[0]["industry"]


# --- production trigger contract -------------------------------------------


def test_trigger_uses_the_persisted_collector_id_and_source_url(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    handler = handler_for(dataset)

    run(db_session, collector, handler, settings=brightdata_settings)

    trigger = handler.trigger_requests[0]
    assert trigger.url.params["collector"] == "c_fix_my_itch"
    assert trigger.url.params["queue_next"] == "1"
    assert b"https://razorpay.com/m/fix-my-itch/" in trigger.content


def test_trigger_never_sends_version_dev(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    # version=dev runs an unpublished draft collector, never production.
    handler = handler_for(dataset)

    run(db_session, collector, handler, settings=brightdata_settings)

    for request in handler.requests:
        assert "version" not in request.url.params


def test_trigger_never_sends_a_deadline(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    # A Bright Data `deadline` once terminated a real production run
    # mid-collection. The local orchestration timeout must stay local.
    handler = handler_for(dataset)

    run(
        db_session,
        collector,
        handler,
        polling=PollingPolicy(interval_seconds=1.0, timeout_seconds=5.0),
        settings=brightdata_settings,
    )

    for request in handler.requests:
        assert "deadline" not in request.url.params
        assert b"deadline" not in request.content


# --- polling ---------------------------------------------------------------


def test_polling_waits_through_multiple_non_terminal_responses(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    handler = handler_for(dataset, building_polls=3)
    clock = FakeClock()

    result = run(
        db_session, collector, handler, clock=clock, settings=brightdata_settings
    )

    assert result.status is RunStatus.SUCCEEDED
    # 3 building polls + 1 terminal poll + 1 dataset fetch.
    assert handler.get_count == 5
    assert clock.slept == [1.0, 1.0, 1.0]


def test_local_timeout_stops_the_run_without_ingesting(
    db_session: Session,
    collector: Collector,
    brightdata_settings: Settings,
) -> None:
    handler = ScriptedBrightData(
        get_responses=[httpx.Response(200, json={"status": "building"})]
    )
    clock = FakeClock()

    with pytest.raises(CollectionTimeoutError) as excinfo:
        run(
            db_session,
            collector,
            handler,
            polling=PollingPolicy(interval_seconds=2.0, timeout_seconds=6.0),
            clock=clock,
            settings=brightdata_settings,
        )

    assert excinfo.value.timeout_seconds == 6.0
    assert signal_count(db_session) == 0
    run_row = only_run(db_session)
    assert run_row.status is RunStatus.FAILED
    assert run_row.raw_metadata["orchestration"]["stage"] == "timeout"
    assert run_row.completed_at is not None


# --- provider failures -----------------------------------------------------


def test_trigger_failure_ingests_nothing_and_records_no_run(
    db_session: Session,
    collector: Collector,
    brightdata_settings: Settings,
) -> None:
    handler = ScriptedBrightData(
        get_responses=[httpx.Response(200, json=[])],
        trigger_response=httpx.Response(500, json={"error": "boom"}),
    )

    with pytest.raises(CollectionTriggerError) as excinfo:
        run(db_session, collector, handler, settings=brightdata_settings)

    assert excinfo.value.__cause__ is not None
    assert signal_count(db_session) == 0
    # No Bright Data collection id exists yet, so no run row can be
    # written without inventing one.
    assert db_session.execute(select(CollectorRun)).scalars().all() == []


def test_dataset_fetch_failure_ingests_nothing(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    handler = ScriptedBrightData(
        get_responses=[
            httpx.Response(200, json=dataset),  # terminal poll succeeds
            httpx.Response(503, json={"error": "unavailable"}),  # fetch fails
        ]
    )

    with pytest.raises(CollectionExecutionError):
        run(db_session, collector, handler, settings=brightdata_settings)

    assert signal_count(db_session) == 0
    run_row = only_run(db_session)
    assert run_row.status is RunStatus.FAILED
    assert run_row.raw_metadata["orchestration"]["stage"] == "collection"


def test_unreadable_status_payload_ingests_nothing(
    db_session: Session,
    collector: Collector,
    brightdata_settings: Settings,
) -> None:
    handler = ScriptedBrightData(
        get_responses=[httpx.Response(200, json={"status": "who knows"})]
    )

    with pytest.raises(MalformedCollectionPayloadError):
        run(db_session, collector, handler, settings=brightdata_settings)

    assert signal_count(db_session) == 0
    assert only_run(db_session).raw_metadata["orchestration"]["stage"] == "payload"


@pytest.mark.parametrize("bad_row", ["not-a-record", None, 7, ["a"]])
def test_malformed_dataset_row_fails_the_run_without_ingesting(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
    bad_row: object,
) -> None:
    # A row that is not a record object means the payload is not the
    # dataset this collection is supposed to produce. The client rejects
    # it outright rather than dropping it, so the good rows around it are
    # never ingested as if the dataset were merely smaller.
    handler = ScriptedBrightData(
        get_responses=[httpx.Response(200, json=[*dataset, bad_row])]
    )

    with pytest.raises(MalformedCollectionPayloadError) as excinfo:
        run(db_session, collector, handler, settings=brightdata_settings)

    cause = excinfo.value.__cause__
    assert isinstance(cause, BrightDataMalformedDatasetError)
    assert cause.index == len(dataset)
    assert signal_count(db_session) == 0
    run_row = only_run(db_session)
    assert run_row.status is RunStatus.FAILED
    assert run_row.raw_metadata["orchestration"]["stage"] == "payload"


# --- JSONL datasets ---------------------------------------------------------


def jsonl_response(records: list[dict[str, Any]]) -> httpx.Response:
    """A completed dataset in the serialization production actually used."""
    return httpx.Response(
        200,
        content="\n".join(json.dumps(record) for record in records) + "\n",
        headers={"Content-Type": "application/jsonl; charset=utf-8"},
    )


def test_a_jsonl_dataset_collects_end_to_end(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    handler = ScriptedBrightData(get_responses=[jsonl_response(dataset)])

    result = run(db_session, collector, handler, settings=brightdata_settings)

    assert result.status is RunStatus.SUCCEEDED
    assert result.fetched_record_count == len(dataset)
    assert result.accepted == len(dataset)
    assert signal_count(db_session) == len(dataset)


def test_a_jsonl_dataset_carrying_the_tam_fault_is_still_rejected(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    # The transport learning to read JSONL must not launder the payload:
    # a tam_score of 70 has to reach the source validator as 70 and be
    # rejected there, which is what makes the drift detectable later.
    corrupted = [
        {**record, "tam_score": record["tam_score"] * 10} for record in dataset
    ]
    handler = ScriptedBrightData(get_responses=[jsonl_response(corrupted)])

    with pytest.raises(SourceContractValidationError) as excinfo:
        run(db_session, collector, handler, settings=brightdata_settings)

    assert len(excinfo.value.report.invalid) == len(dataset)
    assert (
        excinfo.value.report.invalid[0].raw["tam_score"] == dataset[0]["tam_score"] * 10
    )
    assert signal_count(db_session) == 0
    assert only_run(db_session).status is RunStatus.FAILED


# --- fail-closed source validation -----------------------------------------


def assert_batch_rejected(
    db_session: Session,
    collector: Collector,
    records: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> SourceContractValidationError:
    handler = ScriptedBrightData(get_responses=[httpx.Response(200, json=records)])

    with pytest.raises(SourceContractValidationError) as excinfo:
        run(db_session, collector, handler, settings=brightdata_settings)

    assert signal_count(db_session) == 0
    run_row = only_run(db_session)
    assert run_row.status is RunStatus.FAILED
    assert run_row.raw_metadata["orchestration"]["stage"] == "source_validation"
    return excinfo.value


def test_one_invalid_record_blocks_the_entire_batch(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    dataset[1]["severity_score"] = 42

    error = assert_batch_rejected(db_session, collector, dataset, brightdata_settings)

    assert error.fetched_record_count == len(dataset)
    assert len(error.report.invalid) == 1
    assert error.report.invalid[0].index == 1
    assert len(error.report.valid) == len(dataset) - 1


def test_historical_tam_score_60_blocks_the_entire_batch(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    dataset[0]["tam_score"] = 60

    error = assert_batch_rejected(db_session, collector, dataset, brightdata_settings)

    # Never repaired into 6, and the raw value survives as evidence.
    assert error.report.invalid[0].raw["tam_score"] == 60
    evidence = error.evidence()
    assert evidence["invalid_records"][0]["raw"]["tam_score"] == 60


def test_unexpected_source_field_blocks_the_entire_batch(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    dataset[2]["confidence_score"] = 4

    error = assert_batch_rejected(db_session, collector, dataset, brightdata_settings)

    assert error.report.invalid[0].index == 2


def test_validation_failure_preserves_evidence_on_the_run(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    dataset[0]["source"] = "arxiv"

    assert_batch_rejected(db_session, collector, dataset, brightdata_settings)

    orchestration = only_run(db_session).raw_metadata["orchestration"]
    assert orchestration["invalid_record_count"] == 1
    assert orchestration["fetched_record_count"] == len(dataset)
    assert orchestration["invalid_records"][0]["reason"] == "invalid_source"


def test_unknown_industry_is_collected_normally(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    dataset[0]["industry"] = "Autonomous Yak Grooming"

    result = run(
        db_session, collector, handler_for(dataset), settings=brightdata_settings
    )

    assert result.accepted == len(dataset)
    assert signal_count(db_session) == len(dataset)


# --- dynamic dataset size --------------------------------------------------


@pytest.mark.parametrize("size", [1, 3, 7])
def test_record_count_is_dynamic(
    db_session: Session,
    collector: Collector,
    healthy_records: list[dict[str, Any]],
    brightdata_settings: Settings,
    size: int,
) -> None:
    records = [dict(record) for record in healthy_records[:size]]

    result = run(
        db_session, collector, handler_for(records), settings=brightdata_settings
    )

    assert result.fetched_record_count == size
    assert result.accepted == size


def test_empty_dataset_completes_without_ingesting_anything(
    db_session: Session,
    collector: Collector,
    brightdata_settings: Settings,
) -> None:
    # "The scraper produced nothing" is an execution outcome, not a parse
    # failure. Judging whether that is acceptable belongs to RecallGuard.
    result = run(db_session, collector, handler_for([]), settings=brightdata_settings)

    assert result.status is RunStatus.SUCCEEDED
    assert result.fetched_record_count == 0
    assert signal_count(db_session) == 0


def test_healthy_fixture_integrity(healthy_records: list[dict[str, Any]]) -> None:
    # Pins what the committed fixture currently holds. This is a fixture
    # integrity check only -- production behavior stays dynamic and no
    # orchestration test may require this number.
    assert len(healthy_records) == 133


# --- duplicates and idempotency --------------------------------------------


def test_source_duplicates_are_reported_and_ingested_once(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    records = [*dataset, dict(dataset[0])]

    result = run(
        db_session, collector, handler_for(records), settings=brightdata_settings
    )

    assert result.fetched_record_count == len(records)
    assert result.valid_record_count == len(records)
    assert result.source_duplicate_count == 1
    assert result.accepted == len(dataset)
    assert result.duplicates == 1
    assert signal_count(db_session) == len(dataset)


def test_rerunning_the_same_dataset_is_idempotent(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    first = run(
        db_session, collector, handler_for(dataset), settings=brightdata_settings
    )

    second_handler = ScriptedBrightData(
        get_responses=building_then(dataset),
        trigger_response=httpx.Response(200, json={"collection_id": "j_second_run"}),
    )
    second = run(db_session, collector, second_handler, settings=brightdata_settings)

    assert first.accepted == len(dataset)
    assert second.accepted == 0
    assert second.duplicates == len(dataset)
    assert second.status is RunStatus.SUCCEEDED
    assert signal_count(db_session) == len(dataset)


# --- ingestion contract drift ----------------------------------------------


def test_ingestion_rejection_fails_the_run_and_persists_no_signals(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    # The source contract sets no maximum problem length, but Signal.title
    # is limited to 1024 characters -- so this record passes source
    # validation and is then rejected by the generic ingestion pipeline.
    # That drift must fail the run, and the run's other records must not
    # survive: a FAILED run leaves no newly persisted signals.
    dataset[1]["problem"] = "Why " + ("x" * 1100) + "?"

    with pytest.raises(CollectionIngestionError) as excinfo:
        run(db_session, collector, handler_for(dataset), settings=brightdata_settings)

    assert excinfo.value.rejected[0]["index"] == 1
    assert signal_count(db_session) == 0
    db_session.rollback()
    assert signal_count(db_session) == 0
    run_row = only_run(db_session)
    assert run_row.status is RunStatus.FAILED
    assert run_row.raw_metadata["orchestration"]["stage"] == "ingestion"
    # The rejection evidence survives on the run for later diagnosis.
    assert run_row.raw_metadata["orchestration"]["rejected_records"][0]["index"] == 1


def test_database_failure_mid_ingestion_leaves_no_signals(
    db_session: Session,
    engine: Engine,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    """A database error after earlier rows were already flushed."""
    flushes = {"count": 0}

    @event.listens_for(Session, "after_flush")
    def fail_after_the_first_flush(session: Session, _context: Any) -> None:
        if not any(isinstance(obj, Signal) for obj in session.new):
            return
        flushes["count"] += 1
        if flushes["count"] == 2:
            raise InjectedDatabaseError("injected database failure")

    try:
        with pytest.raises(CollectionIngestionError):
            run(
                db_session,
                collector,
                handler_for(dataset),
                settings=brightdata_settings,
            )
    finally:
        event.remove(Session, "after_flush", fail_after_the_first_flush)

    # The first record's INSERT had already been flushed when the failure
    # hit; the rollback must take it back out with the rest.
    assert flushes["count"] == 2
    assert signal_count(db_session) == 0
    run_row = only_run(db_session)
    assert run_row.status is RunStatus.FAILED
    assert run_row.raw_metadata["orchestration"]["stage"] == "ingestion"


def test_success_commits_signals_and_the_terminal_run_together(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    result = run(
        db_session, collector, handler_for(dataset), settings=brightdata_settings
    )

    # Rolling back afterwards proves both sides were durably committed in
    # the same transaction, not merely pending in this session.
    db_session.rollback()
    assert signal_count(db_session) == len(dataset)
    run_row = db_session.get(CollectorRun, result.collector_run_id)
    assert run_row is not None
    assert run_row.status is RunStatus.SUCCEEDED


# --- scope guard -----------------------------------------------------------


def test_result_and_run_carry_no_trust_or_health_concepts(
    db_session: Session,
    collector: Collector,
    dataset: list[dict[str, Any]],
    brightdata_settings: Settings,
) -> None:
    # RecallGuard owns trust. A completed collection must not imply
    # HEALTHY / TRUSTED / RECOVERED anywhere in this layer.
    result = run(
        db_session, collector, handler_for(dataset), settings=brightdata_settings
    )

    forbidden = {"healthy", "trusted", "recovered", "degraded", "confidence"}
    assert forbidden.isdisjoint(CollectionRunResult.model_fields)
    run_row = db_session.get(CollectorRun, result.collector_run_id)
    assert run_row is not None
    assert forbidden.isdisjoint(run_row.raw_metadata["orchestration"])
    assert run_row.status in set(RunStatus)
