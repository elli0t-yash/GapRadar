import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from pydantic import ValidationError

from app.domain.enums import RunStatus
from app.schemas.collector_run import (
    CollectorRunCreate,
    CollectorRunRead,
    CollectorRunUpdate,
)


def test_collector_run_create_valid() -> None:
    started = datetime.now(UTC)
    run = CollectorRunCreate(
        collector_id=uuid.uuid4(),
        external_run_id="run-1",
        started_at=started,
        completed_at=started + timedelta(minutes=5),
        record_count=42,
        raw_metadata={"provider_status": "ok"},
    )

    assert run.status is RunStatus.PENDING
    assert run.record_count == 42


def test_collector_run_create_rejects_negative_record_count() -> None:
    with pytest.raises(ValidationError):
        CollectorRunCreate(
            collector_id=uuid.uuid4(),
            external_run_id="run-1",
            record_count=-1,
        )


def test_collector_run_create_rejects_completed_before_started() -> None:
    started = datetime.now(UTC)
    with pytest.raises(ValidationError):
        CollectorRunCreate(
            collector_id=uuid.uuid4(),
            external_run_id="run-1",
            started_at=started,
            completed_at=started - timedelta(minutes=1),
        )


def test_collector_run_create_metadata_round_trips() -> None:
    run = CollectorRunCreate(
        collector_id=uuid.uuid4(),
        external_run_id="run-1",
        raw_metadata={"raw_score": 0.5, "tags": ["a", "b"]},
    )

    assert run.raw_metadata == {"raw_score": 0.5, "tags": ["a", "b"]}


def test_collector_run_update_rejects_completed_before_started_when_both_present() -> (
    None
):
    started = datetime.now(UTC)
    with pytest.raises(ValidationError):
        CollectorRunUpdate(
            started_at=started,
            completed_at=started - timedelta(minutes=1),
        )


def test_collector_run_update_allows_completed_at_alone() -> None:
    # Ordering against a value that only exists in the database (not in
    # this payload) is a service-layer concern, not enforced here.
    update = CollectorRunUpdate(completed_at=datetime.now(UTC))

    assert update.started_at is None


def test_collector_run_read_serializes_from_orm_like_object() -> None:
    class FakeORMCollectorRun:
        id = uuid.uuid4()
        collector_id = uuid.uuid4()
        external_run_id = "run-1"
        status = RunStatus.SUCCEEDED
        started_at = datetime.now(UTC)
        completed_at = datetime.now(UTC)
        record_count = 10
        raw_metadata: ClassVar = {"provider_status": "ok"}
        created_at = datetime.now(UTC)

    read = CollectorRunRead.model_validate(FakeORMCollectorRun(), from_attributes=True)

    assert read.raw_metadata == {"provider_status": "ok"}
    assert read.status is RunStatus.SUCCEEDED
