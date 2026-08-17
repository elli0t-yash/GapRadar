import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.enums import SourceType
from app.schemas.source import SourceCreate, SourceRead, SourceUpdate


def test_source_create_valid() -> None:
    source = SourceCreate(
        name="Reddit r/startups",
        source_type=SourceType.FORUM,
        base_url="https://reddit.com/r/startups",
    )

    assert source.name == "Reddit r/startups"
    assert source.active is True
    assert str(source.base_url).startswith("https://reddit.com/r/startups")


def test_source_create_rejects_invalid_url() -> None:
    with pytest.raises(ValidationError):
        SourceCreate(
            name="Bad source",
            source_type=SourceType.WEB,
            base_url="not-a-url",
        )


def test_source_create_rejects_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        SourceCreate(
            name="FTP source",
            source_type=SourceType.WEB,
            base_url="ftp://example.com/data",
        )


def test_source_create_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        SourceCreate(
            name="   ",
            source_type=SourceType.WEB,
            base_url="https://example.com",
        )


def test_source_create_forbids_id_and_timestamps() -> None:
    payload = {
        "id": str(uuid.uuid4()),
        "name": "Should be ignored fields",
        "source_type": SourceType.WEB,
        "base_url": "https://example.com",
        "created_at": datetime.now(UTC).isoformat(),
    }
    source = SourceCreate.model_validate(payload)

    assert not hasattr(source, "id")
    assert not hasattr(source, "created_at")


def test_source_update_allows_partial_fields() -> None:
    update = SourceUpdate(active=False)

    assert update.name is None
    assert update.active is False


def test_source_update_rejects_blank_name_when_provided() -> None:
    with pytest.raises(ValidationError):
        SourceUpdate(name="   ")


def test_source_read_serializes_from_orm_like_object() -> None:
    class FakeORMSource:
        id = uuid.uuid4()
        name = "Reddit r/startups"
        source_type = SourceType.FORUM
        base_url = "https://reddit.com/r/startups"
        active = True
        created_at = datetime.now(UTC)
        updated_at = datetime.now(UTC)

    read = SourceRead.model_validate(FakeORMSource(), from_attributes=True)

    assert read.id == FakeORMSource.id
    assert read.source_type is SourceType.FORUM
    assert read.model_dump()["source_type"] == SourceType.FORUM
