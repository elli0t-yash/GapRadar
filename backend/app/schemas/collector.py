from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import CollectorStatus
from app.schemas._validators import non_blank


class CollectorCreate(BaseModel):
    source_id: UUID
    provider: str = Field(min_length=1, max_length=100)
    external_collector_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    status: CollectorStatus = CollectorStatus.ACTIVE

    @field_validator("provider", "external_collector_id", "name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        return non_blank(value)


class CollectorUpdate(BaseModel):
    provider: str | None = Field(default=None, max_length=100)
    external_collector_id: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    status: CollectorStatus | None = None

    @field_validator("provider", "external_collector_id", "name")
    @classmethod
    def not_blank(cls, value: str | None) -> str | None:
        return None if value is None else non_blank(value)


class CollectorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: UUID
    provider: str
    external_collector_id: str
    name: str
    status: CollectorStatus
    created_at: datetime
    updated_at: datetime
