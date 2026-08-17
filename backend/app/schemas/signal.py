from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
)

from app.domain.enums import SignalType
from app.schemas._validators import non_blank


class SignalCreate(BaseModel):
    source_id: UUID
    collector_run_id: UUID
    external_id: str = Field(min_length=1, max_length=255)
    canonical_url: HttpUrl
    title: str = Field(max_length=1024)
    body: str
    signal_type: SignalType
    # Untrusted provider payload. Stored as-is; must never drive control flow.
    metadata: dict[str, Any] | None = None
    observed_at: datetime

    @field_validator("external_id")
    @classmethod
    def external_id_not_blank(cls, value: str) -> str:
        return non_blank(value)

    @field_validator("title", "body")
    @classmethod
    def strip_surrounding_whitespace(cls, value: str) -> str:
        # Conservative normalization only: trim surrounding whitespace.
        # Do not otherwise alter, truncate, or reformat provider content.
        return value.strip()

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class SignalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    source_id: UUID
    collector_run_id: UUID
    external_id: str
    canonical_url: HttpUrl
    title: str
    body: str
    signal_type: SignalType
    # The ORM attribute is `signal_metadata` (the DB column is named
    # "metadata", which SQLAlchemy's declarative base reserves as a Python
    # attribute name). The API contract exposes this as "metadata".
    metadata: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("signal_metadata", "metadata"),
    )
    observed_at: datetime
    created_at: datetime
