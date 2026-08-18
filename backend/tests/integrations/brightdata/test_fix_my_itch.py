import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.models import CollectorRun, Signal, Source
from app.domain.enums import SignalType
from app.ingestion.normalizer import normalize_record
from app.ingestion.service import ingest_collector_output
from app.integrations.brightdata.fix_my_itch import (
    FIX_MY_ITCH_SOURCE_URL,
    FixMyItchInput,
    FixMyItchRecord,
    FixMyItchRejectionReason,
    compute_external_id,
    to_raw_provider_record,
    validate_dataset,
)

SOURCE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OBSERVED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

COMPONENT_SCORE_FIELDS = (
    "severity_score",
    "tam_score",
    "whitespace_score",
    "frequency_score",
)


def only_invalid(
    raw: dict[str, Any],
) -> tuple[FixMyItchRejectionReason, str, Any]:
    report = validate_dataset([raw], source_id=SOURCE_ID)
    assert report.valid == []
    assert len(report.invalid) == 1
    rejected = report.invalid[0]
    return rejected.reason, rejected.detail, rejected.raw


# --- valid production data ------------------------------------------------


def test_valid_production_record_is_accepted(fix_my_itch_record: dict) -> None:
    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.invalid == []
    assert report.duplicates == []
    assert len(report.valid) == 1
    record = report.valid[0].record
    assert record.problem == fix_my_itch_record["problem"]
    assert record.industry == fix_my_itch_record["industry"]
    assert record.source == "fix_my_itch"
    assert record.source_url == FIX_MY_ITCH_SOURCE_URL


def test_healthy_fixture_validates_without_rejections_or_duplicates(
    fix_my_itch_healthy_records: list[dict[str, Any]],
) -> None:
    report = validate_dataset(fix_my_itch_healthy_records, source_id=SOURCE_ID)

    assert report.invalid == []
    assert report.duplicates == []
    # No expectation is asserted about record count, industry count, or
    # rows per industry -- all three are dynamic on the source.
    assert len(report.valid) == len(fix_my_itch_healthy_records)
    assert len({valid.record.industry for valid in report.valid}) >= 1


# --- unknown-field handling ----------------------------------------------


def test_known_provider_input_echo_is_accepted(fix_my_itch_record: dict) -> None:
    # Bright Data echoes the collector's own `input` object onto each row.
    assert fix_my_itch_record["input"] == {"url": FIX_MY_ITCH_SOURCE_URL}

    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.invalid == []
    assert report.valid[0].record.input == FixMyItchInput(url=FIX_MY_ITCH_SOURCE_URL)


@pytest.mark.parametrize("absent", ["omitted", "null"])
def test_missing_provider_input_echo_is_accepted(
    fix_my_itch_record: dict, absent: str
) -> None:
    if absent == "omitted":
        del fix_my_itch_record["input"]
    else:
        fix_my_itch_record["input"] = None

    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.invalid == []
    assert report.valid[0].record.input is None


@pytest.mark.parametrize(
    "url",
    [
        "https://razorpay.com/m/fix-my-itch",
        "https://evil.example.com/m/fix-my-itch/",
        "",
    ],
)
def test_wrong_input_echo_url_is_rejected(fix_my_itch_record: dict, url: str) -> None:
    fix_my_itch_record["input"] = {"url": url}

    reason, detail, raw = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_RECORD
    assert "input.url" in detail
    assert raw["input"] == {"url": url}


def test_unknown_top_level_field_is_rejected(fix_my_itch_record: dict) -> None:
    # An unannounced upstream schema change must surface, not be discarded.
    fix_my_itch_record["confidence_score"] = 4

    reason, detail, raw = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_RECORD
    assert "confidence_score" in detail
    assert raw["confidence_score"] == 4


def test_unknown_field_inside_input_echo_is_rejected(fix_my_itch_record: dict) -> None:
    fix_my_itch_record["input"] = {"url": FIX_MY_ITCH_SOURCE_URL, "page": 2}

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_RECORD
    assert "input.page" in detail


def test_non_object_input_echo_is_rejected(fix_my_itch_record: dict) -> None:
    fix_my_itch_record["input"] = FIX_MY_ITCH_SOURCE_URL

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_RECORD
    assert "input" in detail


def test_unknown_industry_is_accepted(fix_my_itch_record: dict) -> None:
    fix_my_itch_record["industry"] = "Autonomous Yak Grooming"

    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.invalid == []
    assert report.valid[0].record.industry == "Autonomous Yak Grooming"


@pytest.mark.parametrize("itch_score", [0, 0.0, 57.3, 100, 100.0])
def test_itch_score_bounds_are_inclusive(
    fix_my_itch_record: dict, itch_score: float
) -> None:
    fix_my_itch_record["itch_score"] = itch_score

    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.invalid == []
    assert report.valid[0].record.itch_score == itch_score


@pytest.mark.parametrize("field", COMPONENT_SCORE_FIELDS)
@pytest.mark.parametrize("value", [1, 1.0, 7.5, 10, 10.0])
def test_component_score_bounds_are_inclusive(
    fix_my_itch_record: dict, field: str, value: float
) -> None:
    fix_my_itch_record[field] = value

    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.invalid == []
    assert getattr(report.valid[0].record, field) == value


# --- score invariants -----------------------------------------------------


@pytest.mark.parametrize("itch_score", [-0.1, -1, 100.1, 101])
def test_itch_score_outside_0_100_is_rejected(
    fix_my_itch_record: dict, itch_score: float
) -> None:
    fix_my_itch_record["itch_score"] = itch_score

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SCORE
    assert "itch_score" in detail


@pytest.mark.parametrize("field", COMPONENT_SCORE_FIELDS)
@pytest.mark.parametrize("value", [0, 0.9, 10.1, 11])
def test_component_score_outside_1_10_is_rejected(
    fix_my_itch_record: dict, field: str, value: float
) -> None:
    fix_my_itch_record[field] = value

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SCORE
    assert field in detail


@pytest.mark.parametrize("field", ("itch_score", *COMPONENT_SCORE_FIELDS))
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_score_is_rejected(
    fix_my_itch_record: dict, field: str, value: float
) -> None:
    fix_my_itch_record[field] = value

    reason, _, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SCORE


def test_numeric_string_score_is_rejected_not_coerced(
    fix_my_itch_record: dict,
) -> None:
    fix_my_itch_record["tam_score"] = "7"

    reason, _, raw = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SCORE
    assert raw["tam_score"] == "7"


# --- historical TAM x10 bug ----------------------------------------------


@pytest.mark.parametrize("tam_score", [60, 70, 80, 90, 100])
def test_historical_tam_x10_values_fail_validation_and_are_not_rescaled(
    fix_my_itch_record: dict, tam_score: int
) -> None:
    """Regression: the old scraper emitted TAM on a 0..100 scale.

    The backend must expose that as bad upstream data, never quietly
    divide it by ten into a plausible 1..10 value.
    """
    fix_my_itch_record["tam_score"] = tam_score

    reason, detail, raw = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SCORE
    assert "tam_score" in detail
    # The raw record is preserved verbatim: no repair, no rescaling.
    assert raw["tam_score"] == tam_score

    with pytest.raises(ValidationError):
        FixMyItchRecord.model_validate(fix_my_itch_record)


def test_tam_score_60_is_never_turned_into_6(fix_my_itch_record: dict) -> None:
    fix_my_itch_record["tam_score"] = 60

    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)

    assert report.valid == []
    assert [record.raw["tam_score"] for record in report.invalid] == [60]


# --- required text and source identity ------------------------------------


@pytest.mark.parametrize("field", ["problem", "description", "industry"])
def test_missing_text_field_is_rejected(fix_my_itch_record: dict, field: str) -> None:
    del fix_my_itch_record[field]

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.MISSING_REQUIRED_FIELD
    assert field in detail


@pytest.mark.parametrize("field", ["problem", "description", "industry"])
@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_blank_text_field_is_rejected(
    fix_my_itch_record: dict, field: str, value: str
) -> None:
    fix_my_itch_record[field] = value

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.MISSING_REQUIRED_FIELD
    assert field in detail


@pytest.mark.parametrize("source", ["arxiv", "FIX_MY_ITCH", "", None])
def test_wrong_source_is_rejected(fix_my_itch_record: dict, source: Any) -> None:
    fix_my_itch_record["source"] = source

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SOURCE
    assert "source" in detail


@pytest.mark.parametrize(
    "source_url",
    [
        "https://razorpay.com/m/fix-my-itch",
        "https://razorpay.com/m/fix-my-itch/?utm_source=x",
        "https://evil.example.com/m/fix-my-itch/",
        "",
    ],
)
def test_wrong_source_url_is_rejected(
    fix_my_itch_record: dict, source_url: str
) -> None:
    fix_my_itch_record["source_url"] = source_url

    reason, detail, _ = only_invalid(fix_my_itch_record)

    assert reason is FixMyItchRejectionReason.INVALID_SOURCE_URL
    assert "source_url" in detail


def test_non_object_record_is_rejected(fix_my_itch_record: dict) -> None:
    report = validate_dataset(
        [fix_my_itch_record, ["not", "an", "object"]], source_id=SOURCE_ID
    )

    assert len(report.valid) == 1
    assert len(report.invalid) == 1
    assert report.invalid[0].index == 1
    assert report.invalid[0].reason is FixMyItchRejectionReason.INVALID_RECORD


def test_validation_continues_past_an_invalid_record(
    fix_my_itch_healthy_records: list[dict[str, Any]],
) -> None:
    broken = dict(fix_my_itch_healthy_records[0])
    broken["tam_score"] = 60
    records = [broken, *fix_my_itch_healthy_records[1:3]]

    report = validate_dataset(records, source_id=SOURCE_ID)

    assert [record.index for record in report.invalid] == [0]
    assert [valid.index for valid in report.valid] == [1, 2]


# --- deterministic mapping and identity -----------------------------------


def test_mapping_produces_the_generic_ingestion_contract(
    fix_my_itch_record: dict,
) -> None:
    record = FixMyItchRecord.model_validate(fix_my_itch_record)

    raw = to_raw_provider_record(record, observed_at=OBSERVED_AT)

    assert raw["canonical_url"] == FIX_MY_ITCH_SOURCE_URL
    assert raw["title"] == record.problem
    assert raw["body"] == record.description
    assert raw["signal_type"] == SignalType.COMPLAINT.value
    assert raw["observed_at"] == OBSERVED_AT
    # No external_id: identity comes from the existing deterministic
    # content fingerprint.
    assert "external_id" not in raw


def test_mapping_preserves_scoring_metadata(fix_my_itch_record: dict) -> None:
    record = FixMyItchRecord.model_validate(fix_my_itch_record)

    metadata = to_raw_provider_record(record, observed_at=OBSERVED_AT)["metadata"]

    assert metadata == {
        "source": "fix_my_itch",
        "source_url": FIX_MY_ITCH_SOURCE_URL,
        "industry": fix_my_itch_record["industry"],
        "itch_score": fix_my_itch_record["itch_score"],
        "severity_score": fix_my_itch_record["severity_score"],
        "tam_score": fix_my_itch_record["tam_score"],
        "whitespace_score": fix_my_itch_record["whitespace_score"],
        "frequency_score": fix_my_itch_record["frequency_score"],
    }


def test_mapping_is_deterministic(fix_my_itch_record: dict) -> None:
    record = FixMyItchRecord.model_validate(fix_my_itch_record)

    assert to_raw_provider_record(record, observed_at=OBSERVED_AT) == (
        to_raw_provider_record(record, observed_at=OBSERVED_AT)
    )


def test_mapping_rejects_naive_observed_at(fix_my_itch_record: dict) -> None:
    record = FixMyItchRecord.model_validate(fix_my_itch_record)

    with pytest.raises(ValueError, match="timezone-aware"):
        to_raw_provider_record(
            record, observed_at=datetime.fromisoformat("2026-08-18T12:00:00")
        )


def test_reported_identity_matches_the_ingestion_pipeline(
    fix_my_itch_record: dict,
) -> None:
    report = validate_dataset([fix_my_itch_record], source_id=SOURCE_ID)
    valid = report.valid[0]

    normalized = normalize_record(
        to_raw_provider_record(valid.record, observed_at=OBSERVED_AT),
        source_id=SOURCE_ID,
    )

    assert normalized.external_id == valid.external_id
    assert normalized.external_id == compute_external_id(
        valid.record, source_id=SOURCE_ID
    )


def test_identity_is_stable_across_observation_times(fix_my_itch_record: dict) -> None:
    record = FixMyItchRecord.model_validate(fix_my_itch_record)
    later = datetime(2027, 1, 1, tzinfo=UTC)

    first = normalize_record(
        to_raw_provider_record(record, observed_at=OBSERVED_AT), source_id=SOURCE_ID
    )
    second = normalize_record(
        to_raw_provider_record(record, observed_at=later), source_id=SOURCE_ID
    )

    assert first.external_id == second.external_id


def test_identity_differs_per_problem(
    fix_my_itch_healthy_records: list[dict[str, Any]],
) -> None:
    report = validate_dataset(fix_my_itch_healthy_records, source_id=SOURCE_ID)

    external_ids = [valid.external_id for valid in report.valid]

    assert len(set(external_ids)) == len(external_ids)


def test_identity_is_scoped_to_the_source(fix_my_itch_record: dict) -> None:
    record = FixMyItchRecord.model_validate(fix_my_itch_record)
    other_source_id = uuid.UUID("33333333-3333-3333-3333-333333333333")

    assert compute_external_id(record, source_id=SOURCE_ID) != compute_external_id(
        record, source_id=other_source_id
    )


# --- duplicates -----------------------------------------------------------


def test_duplicate_records_are_reported_against_their_first_occurrence(
    fix_my_itch_record: dict,
) -> None:
    report = validate_dataset(
        [fix_my_itch_record, dict(fix_my_itch_record), dict(fix_my_itch_record)],
        source_id=SOURCE_ID,
    )

    assert report.invalid == []
    assert [(dup.index, dup.first_index) for dup in report.duplicates] == [
        (1, 0),
        (2, 0),
    ]
    assert {dup.external_id for dup in report.duplicates} == {
        report.valid[0].external_id
    }


def test_duplicate_detection_is_deterministic(fix_my_itch_record: dict) -> None:
    records = [fix_my_itch_record, dict(fix_my_itch_record)]

    first = validate_dataset(records, source_id=SOURCE_ID)
    second = validate_dataset(records, source_id=SOURCE_ID)

    assert first == second


# --- end-to-end through the existing ingestion pipeline -------------------


def ingest(
    db_session: Session,
    source: Source,
    collector_run: CollectorRun,
    raw_records: list[dict[str, Any]],
) -> Any:
    report = validate_dataset(raw_records, source_id=source.id)
    assert report.invalid == []
    return ingest_collector_output(
        db_session,
        source_id=source.id,
        collector_run_id=collector_run.id,
        records=[
            to_raw_provider_record(valid.record, observed_at=OBSERVED_AT)
            for valid in report.valid
        ],
    )


def test_mapped_records_persist_through_existing_ingestion(
    db_session: Session,
    fix_my_itch_source: Source,
    fix_my_itch_collector_run: CollectorRun,
    fix_my_itch_healthy_records: list[dict[str, Any]],
) -> None:
    batch = fix_my_itch_healthy_records[:5]

    result = ingest(db_session, fix_my_itch_source, fix_my_itch_collector_run, batch)

    assert result.rejected == []
    assert result.accepted == len(batch)
    signal = db_session.get(Signal, result.persisted_signal_ids[0])
    assert signal is not None
    assert signal.signal_type is SignalType.COMPLAINT
    assert signal.canonical_url == "https://razorpay.com/m/fix-my-itch"
    assert signal.signal_metadata["tam_score"] == batch[0]["tam_score"]
    assert signal.signal_metadata["industry"] == batch[0]["industry"]


def test_reingesting_the_same_payload_is_idempotent(
    db_session: Session,
    fix_my_itch_source: Source,
    fix_my_itch_collector_run: CollectorRun,
    fix_my_itch_healthy_records: list[dict[str, Any]],
) -> None:
    batch = fix_my_itch_healthy_records[:5]

    first = ingest(db_session, fix_my_itch_source, fix_my_itch_collector_run, batch)
    second = ingest(db_session, fix_my_itch_source, fix_my_itch_collector_run, batch)

    assert first.accepted == len(batch)
    assert second.accepted == 0
    assert second.duplicates == len(batch)
    assert second.persisted_signal_ids == []
