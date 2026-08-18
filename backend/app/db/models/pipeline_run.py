import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import PipelineRunStatus, ReliabilityState

if TYPE_CHECKING:
    from app.db.models.collector import Collector
    from app.db.models.collector_run import CollectorRun
    from app.db.models.reliability_incident import ReliabilityIncident


# An execution that has not reached a verdict yet. Defined here, next to
# the index that enforces it, so the application's idea of "active" and
# the database's cannot drift apart: the partial unique index below is
# built from this exact tuple.
ACTIVE_PIPELINE_RUN_STATUSES: tuple[PipelineRunStatus, ...] = (
    PipelineRunStatus.QUEUED,
    PipelineRunStatus.COLLECTING,
    PipelineRunStatus.WAITING_PROVIDER,
    PipelineRunStatus.VALIDATING,
    PipelineRunStatus.INGESTING,
    PipelineRunStatus.VERIFYING,
)

# The index predicate, rendered from the tuple above.
#
# `status.name`, NOT `status.value`. SQLAlchemy's Enum persists the
# member NAME, so this column holds 'WAITING_PROVIDER' even though the
# API serializes 'waiting_provider'. A predicate written against the
# lowercase values matches no row at all, which would leave the index
# present, valid, and enforcing nothing -- the worst possible outcome for
# a constraint whose whole job is to stop a duplicate scrape.
#
# Identical syntax on PostgreSQL and SQLite, so the constraint is real in
# the test suite too rather than something only production discovers.
ACTIVE_PIPELINE_RUN_PREDICATE = "status IN ({})".format(
    ", ".join(f"'{status.name}'" for status in ACTIVE_PIPELINE_RUN_STATUSES)
)


class PipelineRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ONE logical pipeline execution: collect -> evaluate -> (repair) -> verify.

    Separate from CollectorRun on purpose, for three reasons that a
    status column on CollectorRun could not express:

    - One logical execution can span SEVERAL collector runs. A cycle that
      detects drift, repairs the scraper, and proves the repair produces
      a detection run and an independent verification run; both belong to
      one execution.
    - A trigger failure produces NO collector run at all (external_run_id
      is NOT NULL and Bright Data only issues a collection id on a
      successful trigger), yet the execution still happened and still has
      to be reportable.
    - RunStatus means "did this provider execution finish". Widening it
      with VALIDATING/INGESTING/VERIFYING would change what SUCCEEDED
      means to RecallGuard's execution check, which is exactly the
      distinction RecallGuard exists to protect.

    `provider_job_id` is the resume anchor. It is written as soon as
    Bright Data issues a collection id and is what makes the invariant

        one logical pipeline execution -> at most one active Bright Data
        collection job

    enforceable: a resume that finds a provider job id re-polls that job
    and never triggers a second one.
    """

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_collector_id", "collector_id"),
        Index("ix_pipeline_runs_status", "status"),
        # Unique where present. NULLs are distinct in both PostgreSQL and
        # SQLite, so the manual endpoint -- which sends no key -- is
        # unconstrained while a scheduler that does send one gets a hard
        # database guarantee instead of a check that races.
        Index("uq_pipeline_runs_idempotency_key", "idempotency_key", unique=True),
        # AT MOST ONE ACTIVE EXECUTION PER COLLECTOR, enforced by the
        # database rather than by a check in the service.
        #
        # A SELECT-then-INSERT cannot provide this: two requests can both
        # observe "no active run" before either inserts, and both would
        # then claim -- which is one collector, two logical executions,
        # and ultimately two Bright Data collections over the same source.
        # A partial unique index makes the second INSERT fail instead, and
        # the service turns that failure into "you joined the existing
        # run".
        #
        # Partial rather than plain: terminal rows are excluded, so a
        # collector accumulates unlimited history while still being
        # allowed only one execution in flight.
        Index(
            "uq_pipeline_runs_active_collector",
            "collector_id",
            unique=True,
            postgresql_where=text(ACTIVE_PIPELINE_RUN_PREDICATE),
            sqlite_where=text(ACTIVE_PIPELINE_RUN_PREDICATE),
        ),
    )

    collector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collectors.id"), nullable=False
    )
    status: Mapped[PipelineRunStatus] = mapped_column(
        Enum(
            PipelineRunStatus,
            name="pipeline_run_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    # Bright Data's collection id ("j_..."), mirrored from the
    # CollectorRun this execution opened. Held here as well because it
    # must survive a resume even when the collector run row is not the
    # thing being looked up.
    provider_job_id: Mapped[str | None] = mapped_column(String(255))
    # The collection this execution is currently working on. Replaced by
    # the verification run when a repair is proven, so it always points
    # at the run whose result the execution's outcome describes.
    collector_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collector_runs.id")
    )
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reliability_incidents.id")
    )
    # NULL until the execution reaches a terminal state. NULL means "not
    # decided yet", never "untrusted" -- an in-flight refresh has no
    # verdict, and rendering one as false would report a degradation that
    # has not happened.
    trusted: Mapped[bool | None] = mapped_column(Boolean)
    # RecallGuard's verdict about the COLLECTOR at the moment this
    # execution finished. Not the execution's own state.
    reliability_state: Mapped[ReliabilityState | None] = mapped_column(
        Enum(
            ReliabilityState,
            name="reliability_state",
            native_enum=False,
            length=32,
        )
    )
    # Short operator-facing reason this execution could not be carried
    # out. Never a provider credential: the transport layer guarantees
    # its own errors carry none, and nothing here reintroduces one.
    error: Mapped[str | None] = mapped_column(Text)
    # Optional caller-supplied execution key. The manual endpoint sends
    # none; the daily scheduler can send one per collector per window to
    # make a double-fired cron a no-op at the database level rather than
    # by a check that races.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    collector: Mapped["Collector"] = relationship()
    collector_run: Mapped["CollectorRun | None"] = relationship()
    incident: Mapped["ReliabilityIncident | None"] = relationship()
