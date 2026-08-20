import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ResearchSource

if TYPE_CHECKING:
    from app.db.models.investigation import Investigation
    from app.db.models.research_paper import ResearchPaper
    from app.db.models.signal import Signal


class ResearchSearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One research search: who asked, what was asked, and when.

    Exists so four questions have answers that a paper row cannot give,
    because each of them can be true many times over for one paper:

        which GapRadar opportunity generated this search?   -> signal_id
        what query was used?                                -> query
        when was it searched?                               -> searched_at
        which papers came back?                             -> ResearchSearchResult

    EVERY SEARCH BELONGS TO EXACTLY ONE SUBJECT, enforced by a CHECK.

    `signal_id` points at `signals`, not at an "opportunities" table --
    there is none. An Opportunity is a read model computed over a trusted
    Signal (app.opportunity_engine), so the Signal is the only durable
    identity a market search can be attributed to. `investigation_id` is
    the same idea for the other kind of subject: a user-supplied
    hypothesis GapRadar was asked to research.

    Both columns are nullable in the column sense and neither is optional
    in the invariant sense: the constraint permits precisely

        signal_id set, investigation_id null
        investigation_id set, signal_id null

    and rejects both-set and both-null. Both-set would make "which
    problem was this searched for" unanswerable. BOTH-NULL was previously
    allowed and is no longer: an unattributed search row is a provider
    call nobody can explain, cannot be read back through any subject, and
    silently inflates nothing while costing money -- so the table refuses
    to record one rather than accumulating orphans that look like data.

    Two nullable columns rather than a polymorphic (subject_type,
    subject_id) pair, for the reason that decides every such choice in
    this schema: a polymorphic id has no foreign key, so nothing stops it
    pointing at a row that does not exist. The cost of that choice is
    exactly this CHECK, which is cheap and is enforced by the database
    rather than by whichever caller happens to be writing.

    `searched_at` is supplied by the caller rather than defaulted from
    `created_at`, because results can be handed to GapRadar well after
    the provider actually ran the search. It answers "when was this
    searched", while `created_at` answers "when did we write it down".
    """

    __tablename__ = "research_search_runs"
    __table_args__ = (
        # A search belongs to EXACTLY one subject -- not "at most one".
        #
        # Written as an XOR over the two IS NOT NULL tests rather than as
        # a NOT(both) because the two are not the same constraint: the
        # weaker form permits a row that names no subject at all, which
        # is a recorded provider call that no read model can ever surface
        # and no operator can explain.
        #
        # Enforced in the database, not just in the service, because the
        # service is not the only writer a schema outlives.
        CheckConstraint(
            "(signal_id IS NOT NULL) <> (investigation_id IS NOT NULL)",
            name="ck_research_search_runs_single_subject",
        ),
        Index("ix_research_search_runs_signal_id", "signal_id"),
        Index("ix_research_search_runs_investigation_id", "investigation_id"),
        Index("ix_research_search_runs_searched_at", "searched_at"),
        # The natural lookup: "have we searched this query on this source
        # before, and what came back?"
        Index("ix_research_search_runs_source_query", "source", "query"),
    )

    # Nullable on purpose -- see the class docstring.
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    # The other kind of subject. Mutually exclusive with signal_id.
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigations.id")
    )
    source: Mapped[ResearchSource] = mapped_column(
        Enum(ResearchSource, name="research_source", native_enum=False, length=32),
        nullable=False,
        default=ResearchSource.ARXIV,
    )
    # The query text as submitted. Stored verbatim: it is the input, and
    # normalizing it would make two genuinely different searches look
    # like one.
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # The provider's own job/collection id, when the caller knows it.
    # Nullable because ingestion is decoupled from acquisition: records
    # can be handed over from a file with no job attached. An identifier,
    # never a credential.
    provider_job_id: Mapped[str | None] = mapped_column(String(255))

    signal: Mapped["Signal | None"] = relationship()
    investigation: Mapped["Investigation | None"] = relationship()
    results: Mapped[list["ResearchSearchResult"]] = relationship(
        back_populates="search_run"
    )


class ResearchSearchResult(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One paper, as returned by one search, at one position.

    The join that makes "the same paper found by two different queries" a
    fact with two rows rather than a conflict. Papers are upserted; these
    rows only accumulate.

    `position` is the paper's 0-based index in the batch the provider
    returned. Preserved because search-result order is a real signal
    about relevance, and it is lost the moment the batch is unpacked.
    """

    __tablename__ = "research_search_results"
    __table_args__ = (
        # A paper appears at most once per search. Re-ingesting the same
        # batch under the same run is a no-op rather than a duplicate.
        UniqueConstraint(
            "research_search_run_id",
            "research_paper_id",
            name="uq_research_search_results_run_paper",
        ),
        Index("ix_research_search_results_run_id", "research_search_run_id"),
        Index("ix_research_search_results_paper_id", "research_paper_id"),
    )

    research_search_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_search_runs.id"), nullable=False
    )
    research_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_papers.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    search_run: Mapped["ResearchSearchRun"] = relationship(back_populates="results")
    research_paper: Mapped["ResearchPaper"] = relationship(
        back_populates="search_results"
    )
