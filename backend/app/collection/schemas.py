import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import RunStatus


class PollingPolicy(BaseModel):
    """How long this process waits for a collection, and how often it asks.

    Both values are LOCAL to GapRadar. Neither is ever sent to Bright
    Data: passing a `deadline` to the trigger endpoint once terminated a
    real production run mid-collection, and this layer must never
    reintroduce that.
    """

    model_config = ConfigDict(frozen=True)

    interval_seconds: float = Field(default=10.0, gt=0)
    timeout_seconds: float = Field(default=900.0, gt=0)

    @model_validator(mode="after")
    def interval_fits_within_timeout(self) -> Self:
        if self.interval_seconds > self.timeout_seconds:
            raise ValueError("interval_seconds cannot exceed timeout_seconds")
        return self


DEFAULT_POLLING_POLICY = PollingPolicy()


class CollectionRunResult(BaseModel):
    """What one orchestrated collection run did.

    Deliberately free of any trust, health, or confidence field. A
    SUCCEEDED status here means "Bright Data ran the collector, the whole
    dataset satisfied the source contract, and the records reached the
    ingestion pipeline" -- it is not a statement that the data is good,
    complete, or trustworthy. Those judgements belong to RecallGuard,
    which does not exist yet.
    """

    model_config = ConfigDict(frozen=True)

    collector_run_id: uuid.UUID
    # Bright Data's collection id ("j_..."), persisted as
    # CollectorRun.external_run_id.
    external_run_id: str
    status: RunStatus
    fetched_record_count: int = 0
    valid_record_count: int = 0
    invalid_record_count: int = 0
    # Records the source itself repeated, detected by the adapter using
    # the same identity the ingestion pipeline uses.
    source_duplicate_count: int = 0
    accepted: int = 0
    duplicates: int = 0
    persisted_signal_ids: list[uuid.UUID] = Field(default_factory=list)
