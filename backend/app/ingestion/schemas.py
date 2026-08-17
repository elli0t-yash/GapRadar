import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import SignalType

# One raw record as returned by a Bright Data collector's structured
# output (app.integrations.brightdata.schemas.CollectorOutput.records).
# Intentionally left as a plain dict rather than a strict schema: different
# collectors may emit different shapes, and this phase does not add a
# dynamic plugin/mapping framework. This normalizer assumes the record
# already uses GapRadar's own conceptual key names (external_id,
# canonical_url, title, body, signal_type, observed_at, metadata) -- how a
# specific real collector's native output gets mapped onto these names is
# a collector-configuration concern, out of scope here.
RawProviderRecord = dict[str, Any]


class RejectionReason(str, enum.Enum):
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_URL = "invalid_url"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_SIGNAL_TYPE = "invalid_signal_type"
    INVALID_RECORD = "invalid_record"


class NormalizedSignal(BaseModel):
    """Fully validated, normalized, identity-resolved signal, ready for
    direct construction of a Signal ORM row.
    """

    model_config = ConfigDict(frozen=True)

    external_id: str
    canonical_url: str
    title: str
    body: str
    signal_type: SignalType
    observed_at: datetime
    # Untrusted provider payload. Preserved verbatim; must never drive
    # control flow.
    metadata: dict[str, Any] = Field(default_factory=dict)


class RejectedRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    reason: RejectionReason
    detail: str
    # The raw record as received, preserved for debugging. Untrusted.
    raw: RawProviderRecord


class IngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: int = 0
    duplicates: int = 0
    rejected: list[RejectedRecord] = Field(default_factory=list)
    persisted_signal_ids: list[uuid.UUID] = Field(default_factory=list)
