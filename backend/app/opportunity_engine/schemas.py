"""The frontend-facing view of one trusted problem signal."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models import Signal
from app.domain.enums import SignalType
from app.opportunity_engine.scoring import opportunity_score


class Opportunity(BaseModel):
    """One Fix My Itch problem, with its component scores and a score.

    Built from a persisted Signal; every value is either a column or a
    key the source itself published in the untrusted metadata payload.
    `opportunity_score` is the only derived field, and it is None when
    the components needed to compute it are not all present.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    source_id: uuid.UUID
    collector_run_id: uuid.UUID
    signal_type: SignalType
    # Signal.title carries the source's `problem` text; `body` carries
    # `description`. Both names are kept so the frontend can use either.
    title: str
    problem: str
    description: str
    canonical_url: str
    observed_at: datetime

    industry: str | None = None
    itch_score: float | None = None
    severity_score: float | None = None
    tam_score: float | None = None
    whitespace_score: float | None = None
    frequency_score: float | None = None
    source: str | None = None
    source_url: str | None = None

    # Deterministic, computed on read, never stored.
    opportunity_score: float | None = None

    @classmethod
    def from_signal(cls, signal: Signal) -> "Opportunity":
        metadata: dict[str, Any] = signal.signal_metadata or {}
        return cls(
            id=signal.id,
            source_id=signal.source_id,
            collector_run_id=signal.collector_run_id,
            signal_type=signal.signal_type,
            title=signal.title,
            problem=signal.title,
            description=signal.body,
            canonical_url=signal.canonical_url,
            observed_at=signal.observed_at,
            industry=_text(metadata.get("industry")),
            itch_score=_score(metadata.get("itch_score")),
            severity_score=_score(metadata.get("severity_score")),
            tam_score=_score(metadata.get("tam_score")),
            whitespace_score=_score(metadata.get("whitespace_score")),
            frequency_score=_score(metadata.get("frequency_score")),
            source=_text(metadata.get("source")),
            source_url=_text(metadata.get("source_url")),
            opportunity_score=opportunity_score(metadata),
        )


def _text(value: Any) -> str | None:
    """Provider metadata is untrusted: only pass a string through."""
    return value if isinstance(value, str) else None


def _score(value: Any) -> float | None:
    """As above, and never coerce a bool or a numeric string into a score."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
