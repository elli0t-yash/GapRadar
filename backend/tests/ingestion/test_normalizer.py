from datetime import UTC, datetime

import pytest

from app.ingestion.normalizer import (
    RecordRejectedError,
    normalize_record,
    normalize_text,
    normalize_url,
    parse_timestamp,
)
from app.ingestion.schemas import RejectionReason
from tests.ingestion.factories import SOURCE_ID, make_raw_record


def test_normalize_text_trims_and_collapses_whitespace() -> None:
    assert normalize_text("  hello   world  ") == "hello world"


def test_normalize_text_collapses_excess_blank_lines() -> None:
    assert normalize_text("a\n\n\n\n\nb") == "a\n\nb"


def test_normalize_text_never_paraphrases_content() -> None:
    text = "  The   quick brown fox.  \nJumps over."
    assert normalize_text(text) == "The quick brown fox. \nJumps over."


def test_normalize_url_lowercases_scheme_and_host() -> None:
    assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_normalize_url_removes_fragment() -> None:
    assert (
        normalize_url("https://example.com/path#section") == "https://example.com/path"
    )


def test_normalize_url_removes_single_trailing_slash() -> None:
    assert normalize_url("https://example.com/path/") == "https://example.com/path"


def test_normalize_url_keeps_root_slash() -> None:
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_normalize_url_preserves_query_string() -> None:
    url = "https://example.com/path?id=123&sort=new"
    assert normalize_url(url) == url


def test_normalize_url_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        normalize_url("ftp://example.com/file")


def test_normalize_url_rejects_missing_host() -> None:
    with pytest.raises(ValueError, match="missing a host"):
        normalize_url("https:///no-host")


def test_parse_timestamp_accepts_aware_datetime() -> None:
    value = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_timestamp(value) == value


def test_parse_timestamp_converts_to_utc() -> None:
    result = parse_timestamp("2026-01-01T00:00:00+05:00")
    assert result.tzinfo is UTC
    assert result.hour == 19  # previous day in UTC


def test_parse_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_timestamp(datetime(2026, 1, 1))  # noqa: DTZ001


def test_parse_timestamp_rejects_naive_string() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_timestamp("2026-01-01T00:00:00")


def test_parse_timestamp_rejects_unparseable_string() -> None:
    with pytest.raises(ValueError, match="could not parse"):
        parse_timestamp("not-a-timestamp")


def test_parse_timestamp_rejects_wrong_type() -> None:
    with pytest.raises(TypeError, match="unsupported timestamp type"):
        parse_timestamp(12345)


def test_normalize_record_valid() -> None:
    raw = make_raw_record()
    normalized = normalize_record(raw, source_id=SOURCE_ID)

    assert normalized.external_id == "post-1"
    assert normalized.canonical_url == "https://reddit.com/r/startups/post-1"
    assert normalized.title == "I wish there was a tool for X"


def test_normalize_record_rejects_missing_canonical_url() -> None:
    raw = make_raw_record(canonical_url=None)
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.MISSING_REQUIRED_FIELD


def test_normalize_record_rejects_blank_title() -> None:
    raw = make_raw_record(title="   ")
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.MISSING_REQUIRED_FIELD


def test_normalize_record_rejects_blank_body() -> None:
    raw = make_raw_record(body="")
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.MISSING_REQUIRED_FIELD


def test_normalize_record_rejects_missing_signal_type() -> None:
    raw = make_raw_record(signal_type=None)
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.MISSING_REQUIRED_FIELD


def test_normalize_record_rejects_invalid_signal_type() -> None:
    raw = make_raw_record(signal_type="not_a_real_type")
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.INVALID_SIGNAL_TYPE


def test_normalize_record_rejects_invalid_url() -> None:
    raw = make_raw_record(canonical_url="not a url at all")
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.INVALID_URL


def test_normalize_record_rejects_invalid_timestamp() -> None:
    raw = make_raw_record(observed_at="2026-01-01T00:00:00")  # naive
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.INVALID_TIMESTAMP


def test_normalize_record_rejects_missing_observed_at() -> None:
    raw = make_raw_record(observed_at=None)
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.MISSING_REQUIRED_FIELD


def test_normalize_record_rejects_non_dict_metadata() -> None:
    raw = make_raw_record(metadata="not-a-dict")
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record(raw, source_id=SOURCE_ID)
    assert exc_info.value.reason is RejectionReason.INVALID_RECORD


def test_normalize_record_rejects_non_dict_record() -> None:
    with pytest.raises(RecordRejectedError) as exc_info:
        normalize_record("not a dict", source_id=SOURCE_ID)  # type: ignore[arg-type]
    assert exc_info.value.reason is RejectionReason.INVALID_RECORD


def test_normalize_record_preserves_metadata_but_it_is_inert() -> None:
    raw = make_raw_record(
        metadata={"__class__": "malicious", "raw_score": 0.9, "tags": ["urgent"]}
    )
    normalized = normalize_record(raw, source_id=SOURCE_ID)

    assert normalized.metadata == {
        "__class__": "malicious",
        "raw_score": 0.9,
        "tags": ["urgent"],
    }
    assert isinstance(normalized.metadata, dict)


def test_normalize_record_defaults_missing_metadata_to_empty_dict() -> None:
    raw = make_raw_record(metadata=None)
    normalized = normalize_record(raw, source_id=SOURCE_ID)
    assert normalized.metadata == {}
