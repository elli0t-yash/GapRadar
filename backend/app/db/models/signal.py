import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import SignalType
from app.db.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.collector_run import CollectorRun
    from app.db.models.source import Source


class Signal(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (
        # external_id is only guaranteed unique within a given source's
        # namespace, not globally across all sources.
        UniqueConstraint(
            "source_id", "external_id", name="uq_signals_source_external_id"
        ),
        Index("ix_signals_canonical_url", "canonical_url"),
        Index("ix_signals_observed_at", "observed_at"),
        Index("ix_signals_signal_type", "signal_type"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id"), nullable=False, index=True
    )
    collector_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_runs.id"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    signal_type: Mapped[SignalType] = mapped_column(
        Enum(SignalType, name="signal_type", native_enum=False, length=32),
        nullable=False,
    )
    # Untrusted provider payload, stored as-is for audit/debugging.
    # Must never be interpreted or executed by application logic.
    # Column named "metadata" in the database; the Python attribute is
    # renamed because `metadata` is reserved on the declarative base.
    signal_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON().with_variant(JSONB, "postgresql")
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    source: Mapped["Source"] = relationship(back_populates="signals")
    collector_run: Mapped["CollectorRun"] = relationship(back_populates="signals")
