import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import RunStatus
from app.db.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.collector import Collector
    from app.db.models.signal import Signal


class CollectorRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "collector_runs"
    __table_args__ = (
        # external_run_id is only guaranteed unique within a given
        # collector's run history, not globally across all collectors.
        UniqueConstraint(
            "collector_id",
            "external_run_id",
            name="uq_collector_runs_collector_external_run_id",
        ),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collectors.id"), nullable=False, index=True
    )
    external_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status", native_enum=False, length=32),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Untrusted provider payload, stored as-is for audit/debugging.
    # Must never be interpreted or executed by application logic.
    raw_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )

    collector: Mapped["Collector"] = relationship(back_populates="runs")
    signals: Mapped[list["Signal"]] = relationship(back_populates="collector_run")
