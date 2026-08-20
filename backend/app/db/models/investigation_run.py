import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import InvestigationRunStatus, ResearchOutcomeReason

if TYPE_CHECKING:
    from app.db.models.investigation import Investigation


# A run that has not reached a verdict yet. Defined next to the index that
# enforces it so the application's idea of "active" and the database's
# cannot drift apart. Mirrors
# app.db.models.research_enrichment_run.ACTIVE_ENRICHMENT_STATUSES.
ACTIVE_INVESTIGATION_RUN_STATUSES: tuple[InvestigationRunStatus, ...] = (
    InvestigationRunStatus.QUEUED,
    InvestigationRunStatus.RUNNING,
)

# `status.name`, NOT `status.value`. SQLAlchemy's Enum persists the member
# NAME, so this column holds 'RUNNING' while the API serialises 'running'.
# A predicate written against the lowercase values would match no row,
# leaving an index that is present, valid, and enforcing nothing -- the
# worst outcome for a constraint whose entire job is to stop a second
# billable provider run.
ACTIVE_INVESTIGATION_RUN_PREDICATE = "status IN ({})".format(
    ", ".join(f"'{status.name}'" for status in ACTIVE_INVESTIGATION_RUN_STATUSES)
)


class InvestigationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution attempt against one Investigation.

    THE INVESTIGATION IS THE SUBJECT; THIS IS AN ATTEMPT AT IT. Keeping
    them apart is what gives re-runs a history: a second run does not
    overwrite the record of the first, so when two attempts disagree
    there is something to point at. `Investigation.status` mirrors the
    latest run's state for convenience, but this row is authoritative for
    execution history and is the only thing a reconciler touches.

    Deliberately shaped like ResearchEnrichmentRun, field for field,
    because it is the same kind of object and the frontend polls it the
    same way. It is a SEPARATE table rather than a shared one because the
    two point at different subjects with real foreign keys, and a single
    table would need a nullable-pair or a polymorphic id that no database
    can enforce.

    Phase 3 scope: planning, academic research, demand discovery and
    competitor discovery. There is deliberately no whitespace phase and
    no verdict, and no column pretending otherwise -- a phase reporting
    0/0 for work nobody wrote is indistinguishable from a phase that ran
    and found nothing.
    """

    __tablename__ = "investigation_runs"
    __table_args__ = (
        Index("ix_investigation_runs_investigation_id", "investigation_id"),
        Index("ix_investigation_runs_status", "status"),
        # AT MOST ONE ACTIVE RUN PER INVESTIGATION, enforced by the
        # database.
        #
        # A SELECT-then-INSERT cannot provide this: two clicks, two tabs,
        # or a re-rendered effect can all observe "nothing running" before
        # any of them inserts, and every one would then start its own
        # Bright Data searches and its own LLM calls over the same
        # investigation. A partial unique index fails the losers instead,
        # and the service turns that failure into "you joined the run
        # already in flight".
        #
        # Partial rather than plain: terminal rows are excluded, so an
        # investigation accumulates unlimited history while still being
        # allowed only one run in flight.
        Index(
            "uq_investigation_runs_active_investigation",
            "investigation_id",
            unique=True,
            postgresql_where=text(ACTIVE_INVESTIGATION_RUN_PREDICATE),
            sqlite_where=text(ACTIVE_INVESTIGATION_RUN_PREDICATE),
        ),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id"), nullable=False
    )
    status: Mapped[InvestigationRunStatus] = mapped_column(
        Enum(
            InvestigationRunStatus,
            name="investigation_run_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the run could not be carried out. Present only on FAILED, and
    # written to be shown to a user: never a credential, a stack trace, or
    # a prompt.
    error: Mapped[str | None] = mapped_column(Text)
    # A completed-but-incomplete run: some searches returned and some did
    # not. Set alongside SUCCEEDED, never instead of it.
    warning: Mapped[str | None] = mapped_column(Text)
    # Per-query progress, written as the run proceeds. The ONLY source the
    # frontend may use to say "2 of 3 searches complete"; without it the
    # UI would have to fake stage progress on a timer.
    query_states: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    # WHY this run ended as it did, as a value the frontend can branch on.
    # NULL for an ordinary success with matches. Never the thing that
    # decides SUCCEEDED vs FAILED -- `status` remains that.
    outcome_reason: Mapped[ResearchOutcomeReason | None] = mapped_column(
        Enum(
            ResearchOutcomeReason,
            name="research_outcome_reason",
            native_enum=False,
            length=48,
        )
    )
    # The research funnel: discovered / selected / judged / matched.
    # Persisted because three of the four are unrecoverable afterwards --
    # rejected verdicts are not stored.
    counters: Mapped[dict[str, int]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    # PHASE-BY-PHASE PROGRESS, typed by
    # app.investigations.progress.InvestigationRunPhases.
    #
    # A run now has four phases whose numbers mean different things, and
    # "18 judged" is meaningless without knowing whether it counts papers
    # or web pages. `counters` above stays because it is the research
    # funnel and a shipped client contract pins it -- both are written by
    # the same helper from the same source, so they cannot disagree.
    phases: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )

    investigation: Mapped["Investigation"] = relationship()
