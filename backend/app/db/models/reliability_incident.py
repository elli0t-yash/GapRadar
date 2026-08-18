import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import FailureClassification, IncidentStatus, RecommendedAction

if TYPE_CHECKING:
    from app.db.models.collector import Collector
    from app.db.models.collector_run import CollectorRun


class ReliabilityIncident(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One reliability failure, tracked across however many collector runs
    it takes to prove it repaired.

    Separate from CollectorRun on purpose: a run records whether one
    execution completed, while an incident spans a detection run, a
    repair attempt, and a later independent verification run. Folding
    reliability into CollectorRun would conflate "this execution
    succeeded" with "this collector can be trusted" -- exactly the
    distinction RecallGuard exists to keep.

    One row per incident; a healthy collector has no row at all.
    """

    __tablename__ = "reliability_incidents"
    __table_args__ = (
        Index("ix_reliability_incidents_collector_id", "collector_id"),
        Index("ix_reliability_incidents_status", "status"),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collectors.id"), nullable=False
    )
    # The run whose evaluation opened this incident. Nullable because a
    # failure can precede any run row existing (a trigger failure never
    # receives a collection id, so no run can be persisted for it).
    detection_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collector_runs.id")
    )
    # The fresh, independent run that proved recovery. Set only when an
    # incident reaches RECOVERED.
    verification_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collector_runs.id")
    )
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, name="incident_status", native_enum=False, length=32),
        nullable=False,
    )
    classification: Mapped[FailureClassification] = mapped_column(
        Enum(
            FailureClassification,
            name="failure_classification",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    recommended_action: Mapped[RecommendedAction] = mapped_column(
        Enum(
            RecommendedAction, name="recommended_action", native_enum=False, length=32
        ),
        nullable=False,
    )
    # Autonomous repair cycles started for this incident, capped by
    # app.recallguard.service.MAX_AUTONOMOUS_REPAIR_ATTEMPTS.
    repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Deterministic diagnosis: expected vs observed per check, bounded
    # samples of offending records, and the repair/verification history.
    # Written by RecallGuard, never by a provider, and never used to
    # drive control flow.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    # Structured proof that an independent run re-established the
    # contract. Present only on a RECOVERED incident.
    recovery_proof: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )

    collector: Mapped["Collector"] = relationship()
    detection_run: Mapped["CollectorRun | None"] = relationship(
        foreign_keys=[detection_run_id]
    )
    verification_run: Mapped["CollectorRun | None"] = relationship(
        foreign_keys=[verification_run_id]
    )
