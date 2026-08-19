import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ResearchEnrichmentStatus

if TYPE_CHECKING:
    from app.db.models.signal import Signal


# A job that has not reached a verdict yet. Defined next to the index that
# enforces it so the application's idea of "active" and the database's
# cannot drift apart.
ACTIVE_ENRICHMENT_STATUSES: tuple[ResearchEnrichmentStatus, ...] = (
    ResearchEnrichmentStatus.QUEUED,
    ResearchEnrichmentStatus.RUNNING,
)

# `status.name`, NOT `status.value`. SQLAlchemy's Enum persists the member
# NAME, so this column holds 'RUNNING' while the API serialises 'running'.
# A predicate written against the lowercase values would match no row,
# leaving an index that is present, valid, and enforcing nothing -- the
# worst outcome for a constraint whose entire job is to stop a second
# billable provider run.
ACTIVE_ENRICHMENT_PREDICATE = "status IN ({})".format(
    ", ".join(f"'{status.name}'" for status in ACTIVE_ENRICHMENT_STATUSES)
)


class ResearchEnrichmentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One on-demand request to find research for one opportunity.

    Exists so the browser has something to poll. `GET /research` stays a
    pure read of persisted intelligence; this row is the separate,
    explicitly-created record of an acquisition that a user asked for.

    Kept deliberately thin. It records that the work was requested, when
    it ran, and whether it succeeded -- not what it found. What it found
    is the research intelligence itself, which the client refetches on
    success rather than reading a duplicated summary from here that could
    disagree with it.
    """

    __tablename__ = "research_enrichment_runs"
    __table_args__ = (
        Index("ix_research_enrichment_runs_signal_id", "signal_id"),
        Index("ix_research_enrichment_runs_status", "status"),
        # AT MOST ONE ACTIVE JOB PER OPPORTUNITY, enforced by the database.
        #
        # A SELECT-then-INSERT cannot provide this: two clicks, two tabs,
        # or a re-rendered effect can all observe "nothing running" before
        # any of them inserts, and every one would then start its own
        # Bright Data searches and its own LLM calls over the same signal.
        # A partial unique index fails the losers instead, and the service
        # turns that failure into "you joined the job already running".
        #
        # Partial rather than plain: terminal rows are excluded, so an
        # opportunity accumulates unlimited history while still being
        # allowed only one job in flight.
        Index(
            "uq_research_enrichment_runs_active_signal",
            "signal_id",
            unique=True,
            postgresql_where=text(ACTIVE_ENRICHMENT_PREDICATE),
            sqlite_where=text(ACTIVE_ENRICHMENT_PREDICATE),
        ),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signals.id"), nullable=False
    )
    status: Mapped[ResearchEnrichmentStatus] = mapped_column(
        Enum(
            ResearchEnrichmentStatus,
            name="research_enrichment_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the job could not be carried out. Present only on FAILED, and
    # written to be shown to a user: it never carries a provider
    # credential, a stack trace, or a prompt.
    error: Mapped[str | None] = mapped_column(Text)

    signal: Mapped["Signal"] = relationship()
