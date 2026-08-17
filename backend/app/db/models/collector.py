import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import CollectorStatus

if TYPE_CHECKING:
    from app.db.models.collector_run import CollectorRun
    from app.db.models.source import Source


class Collector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "collectors"
    __table_args__ = (
        # external_collector_id is only guaranteed unique within a given
        # provider's namespace (e.g. Bright Data), not globally, so the
        # uniqueness constraint is scoped to (provider, external_collector_id).
        UniqueConstraint(
            "provider",
            "external_collector_id",
            name="uq_collectors_provider_external_collector_id",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    external_collector_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CollectorStatus] = mapped_column(
        Enum(CollectorStatus, name="collector_status", native_enum=False, length=32),
        nullable=False,
    )

    source: Mapped["Source"] = relationship(back_populates="collectors")
    runs: Mapped[list["CollectorRun"]] = relationship(back_populates="collector")
