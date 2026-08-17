import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.enums import CollectorStatus
from app.schemas.collector import CollectorCreate, CollectorRead, CollectorUpdate


def test_collector_create_valid() -> None:
    collector = CollectorCreate(
        source_id=uuid.uuid4(),
        provider="brightdata",
        external_collector_id="collector-123",
        name="Reddit collector",
    )

    assert collector.status is CollectorStatus.ACTIVE


def test_collector_create_rejects_blank_provider() -> None:
    with pytest.raises(ValidationError):
        CollectorCreate(
            source_id=uuid.uuid4(),
            provider="   ",
            external_collector_id="collector-123",
            name="Reddit collector",
        )


def test_collector_create_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        CollectorCreate(
            source_id=uuid.uuid4(),
            provider="brightdata",
            external_collector_id="collector-123",
            name="Reddit collector",
            status="not-a-real-status",
        )


def test_collector_create_rejects_non_uuid_source_id() -> None:
    with pytest.raises(ValidationError):
        CollectorCreate(
            source_id="not-a-uuid",
            provider="brightdata",
            external_collector_id="collector-123",
            name="Reddit collector",
        )


def test_collector_update_allows_partial_fields() -> None:
    update = CollectorUpdate(status=CollectorStatus.PAUSED)

    assert update.provider is None
    assert update.status is CollectorStatus.PAUSED


def test_collector_read_serializes_from_orm_like_object() -> None:
    class FakeORMCollector:
        id = uuid.uuid4()
        source_id = uuid.uuid4()
        provider = "brightdata"
        external_collector_id = "collector-123"
        name = "Reddit collector"
        status = CollectorStatus.ACTIVE
        created_at = datetime.now(UTC)
        updated_at = datetime.now(UTC)

    read = CollectorRead.model_validate(FakeORMCollector(), from_attributes=True)

    assert read.status is CollectorStatus.ACTIVE
    assert isinstance(read.id, uuid.UUID)
