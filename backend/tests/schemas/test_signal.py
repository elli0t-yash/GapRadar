import uuid
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from pydantic import ValidationError

from app.domain.enums import SignalType
from app.schemas.signal import SignalCreate, SignalRead


def make_signal_create(**overrides: object) -> SignalCreate:
    defaults: dict[str, object] = {
        "source_id": uuid.uuid4(),
        "collector_run_id": uuid.uuid4(),
        "external_id": "post-1",
        "canonical_url": "https://reddit.com/r/startups/post-1",
        "title": "I wish there was a tool for X",
        "body": "Full complaint text goes here.",
        "signal_type": SignalType.COMPLAINT,
        "observed_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return SignalCreate(**defaults)


def test_signal_create_valid() -> None:
    signal = make_signal_create()

    assert signal.signal_type is SignalType.COMPLAINT


def test_signal_create_rejects_invalid_canonical_url() -> None:
    with pytest.raises(ValidationError):
        make_signal_create(canonical_url="not-a-url")


def test_signal_create_rejects_non_http_canonical_url() -> None:
    with pytest.raises(ValidationError):
        make_signal_create(canonical_url="ftp://example.com/post-1")


def test_signal_create_rejects_blank_external_id() -> None:
    with pytest.raises(ValidationError):
        make_signal_create(external_id="   ")


def test_signal_create_rejects_timezone_naive_observed_at() -> None:
    with pytest.raises(ValidationError):
        make_signal_create(observed_at=datetime.now())  # noqa: DTZ005


def test_signal_create_metadata_round_trips() -> None:
    signal = make_signal_create(metadata={"raw_score": 0.87, "tags": ["urgent"]})

    assert signal.metadata == {"raw_score": 0.87, "tags": ["urgent"]}


def test_signal_create_strips_surrounding_whitespace_only() -> None:
    signal = make_signal_create(
        title="  I wish there was a tool for X  ",
        body="  Full complaint   text.  ",
    )

    assert signal.title == "I wish there was a tool for X"
    assert signal.body == "Full complaint   text."


def test_signal_read_serializes_from_orm_like_object_with_renamed_metadata_attr() -> (
    None
):
    class FakeORMSignal:
        id = uuid.uuid4()
        source_id = uuid.uuid4()
        collector_run_id = uuid.uuid4()
        external_id = "post-1"
        canonical_url = "https://reddit.com/r/startups/post-1"
        title = "I wish there was a tool for X"
        body = "Full complaint text goes here."
        signal_type = SignalType.COMPLAINT
        # ORM attribute is `signal_metadata` (DB column "metadata"),
        # mirroring app.db.models.signal.Signal.
        signal_metadata: ClassVar = {"raw_score": 0.87}
        observed_at = datetime.now(UTC)
        created_at = datetime.now(UTC)

    read = SignalRead.model_validate(FakeORMSignal(), from_attributes=True)

    assert read.metadata == {"raw_score": 0.87}


def test_signal_read_accepts_metadata_by_json_key_too() -> None:
    read = SignalRead.model_validate(
        {
            "id": uuid.uuid4(),
            "source_id": uuid.uuid4(),
            "collector_run_id": uuid.uuid4(),
            "external_id": "post-1",
            "canonical_url": "https://reddit.com/r/startups/post-1",
            "title": "Title",
            "body": "Body",
            "signal_type": SignalType.COMPLAINT,
            "metadata": {"raw_score": 0.5},
            "observed_at": datetime.now(UTC),
            "created_at": datetime.now(UTC),
        }
    )

    assert read.metadata == {"raw_score": 0.5}
