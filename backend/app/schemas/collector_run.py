from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import RunStatus
from app.schemas._validators import non_blank


def _validate_run_interval(
    started_at: datetime | None, completed_at: datetime | None
) -> None:
    if (
        started_at is not None
        and completed_at is not None
        and completed_at < started_at
    ):
        raise ValueError("completed_at cannot be before started_at")


class CollectorRunCreate(BaseModel):
    collector_id: UUID
    external_run_id: str = Field(min_length=1, max_length=255)
    status: RunStatus = RunStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    record_count: int = Field(default=0, ge=0)
    # Untrusted provider payload. Stored as-is; must never drive control flow.
    raw_metadata: dict[str, Any] | None = None

    @field_validator("external_run_id")
    @classmethod
    def external_run_id_not_blank(cls, value: str) -> str:
        return non_blank(value)

    @model_validator(mode="after")
    def check_run_interval(self) -> Self:
        _validate_run_interval(self.started_at, self.completed_at)
        return self


class CollectorRunUpdate(BaseModel):
    """Partial update of a run's lifecycle fields.

    Only checks started_at/completed_at ordering when both are present in
    the same payload; enforcing the ordering against a value already
    persisted (e.g. setting completed_at when started_at exists only in the
    database) is a service-layer concern, not a schema-layer one.
    """

    status: RunStatus | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    record_count: int | None = Field(default=None, ge=0)
    raw_metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_run_interval(self) -> Self:
        _validate_run_interval(self.started_at, self.completed_at)
        return self


class CollectorRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    collector_id: UUID
    external_run_id: str
    status: RunStatus
    started_at: datetime | None
    completed_at: datetime | None
    record_count: int
    raw_metadata: dict[str, Any] | None
    created_at: datetime
