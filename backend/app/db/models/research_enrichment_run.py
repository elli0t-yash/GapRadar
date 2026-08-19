import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ResearchEnrichmentStatus, ResearchOutcomeReason

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
    # A completed-but-incomplete run: some searches returned and some did
    # not. Set alongside SUCCEEDED, never instead of it -- the research
    # that WAS found is real and is worth showing, and hiding the gap
    # would be the dishonest half of that trade.
    warning: Mapped[str | None] = mapped_column(Text)
    # Per-query progress, written as the run proceeds rather than at the
    # end. This is the ONLY source the frontend may use to say "2 of 3
    # searches complete": without it the UI would have to fake stage
    # progress on a timer, which is exactly what a 14-minute run made
    # unforgivable.
    #
    # A denormalized snapshot, deliberately. The alternative -- a
    # research_enrichment_query table -- buys referential neatness for a
    # payload that is never queried across runs, only read back whole for
    # the run that owns it.
    query_states: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    # WHY this run ended as it did, as a value the frontend can branch on.
    # NULL for an ordinary success with matches, and for rows written
    # before outcome reasons existed. Never the thing that decides
    # SUCCEEDED vs FAILED -- `status` remains that -- only why.
    outcome_reason: Mapped[ResearchOutcomeReason | None] = mapped_column(
        Enum(
            ResearchOutcomeReason,
            name="research_outcome_reason",
            native_enum=False,
            length=48,
        )
    )
    # The four counts that describe one run's funnel, persisted because
    # THREE OF THEM ARE NOT DERIVABLE from anything else afterwards.
    #
    #   discovered -> distinct papers acquired across all searches
    #   selected   -> survivors of the lexical pre-filter and the cap
    #   judged     -> papers the semantic matcher actually returned on
    #   matched    -> papers at or above the relevance threshold
    #
    # Only `matched` has a table of its own; rejected verdicts are not
    # persisted, so without this the UI can only ever report "discovered"
    # and is forced to mislabel it. That mislabelling is exactly how a
    # run showed "34 papers" under "Semantic matching" while its own
    # summary said 20 were reviewed.
    counters: Mapped[dict[str, int]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )

    signal: Mapped["Signal"] = relationship()
