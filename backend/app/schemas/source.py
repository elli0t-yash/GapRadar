from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.domain.enums import SourceType
from app.schemas._validators import non_blank


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    base_url: HttpUrl
    active: bool = True

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return non_blank(value)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    source_type: SourceType | None = None
    base_url: HttpUrl | None = None
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else non_blank(value)


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_type: SourceType
    base_url: HttpUrl
    active: bool
    created_at: datetime
    updated_at: datetime
