import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.research_paper import ResearchPaper
    from app.db.models.signal import Signal


class OpportunityResearchMatch(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One claim that a piece of research is relevant to one opportunity.

    Many-to-many in both directions, and both directions are real: a
    single paper on dynamic vehicle routing can be relevant to several
    logistics opportunities, and one opportunity can be served by several
    papers. Neither side owns the other, which is why this is its own
    table rather than a column on either.

    `signal_id` points at `signals`, not at an "opportunities" table --
    there is none. An Opportunity is a read model computed on read over a
    trusted Signal (app.opportunity_engine.schemas.Opportunity), so the
    Signal row is the only durable identity a match can hang off. A match
    written here therefore survives independently of whether that
    signal's collector is currently trusted; filtering matches down to
    trusted opportunities stays the Opportunity Engine's job, exactly as
    it already is for signals.

    NOTHING WRITES THIS TABLE YET. The matcher is a later phase
    deliberately: the storage and provenance foundation is settled first
    so the matcher has somewhere honest to put its output.
    """

    __tablename__ = "opportunity_research_matches"
    __table_args__ = (
        # One verdict per (opportunity, paper). Re-running the matcher
        # updates the existing row instead of stacking near-duplicate
        # claims, so "how relevant is this paper to this opportunity"
        # always has exactly one answer.
        UniqueConstraint(
            "signal_id",
            "research_paper_id",
            name="uq_opportunity_research_matches_signal_paper",
        ),
        Index("ix_opportunity_research_matches_signal_id", "signal_id"),
        Index("ix_opportunity_research_matches_paper_id", "research_paper_id"),
        # Ranking a single opportunity's matches best-first.
        Index(
            "ix_opportunity_research_matches_signal_relevance",
            "signal_id",
            "relevance_score",
        ),
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signals.id"), nullable=False
    )
    research_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_papers.id"), nullable=False
    )
    # NOT NULL on purpose. A match with no relevance score is an
    # assertion with no evidence, and this project does not store those.
    # Whatever writes a row must say how relevant it thinks the paper is.
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    # How mature the research is -- roughly "could this be built on
    # today". Nullable because it is a genuinely separate judgement from
    # relevance and a matcher may reach one without the other; null means
    # "not assessed", never "not ready".
    technical_readiness_score: Mapped[float | None] = mapped_column(Float)
    # The concepts that actually connected the two, e.g.
    # ["dynamic routing", "fleet dispatch"]. JSON rather than a join
    # table: they are free-text terms produced per match, with no
    # identity or lifecycle of their own, and nothing joins on them.
    matched_concepts: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    # Human-readable justification. Nullable: a match may be produced by
    # a purely numeric method that has no sentence to offer, and an
    # invented explanation would be worse than none.
    match_reason: Mapped[str | None] = mapped_column(Text)

    signal: Mapped["Signal"] = relationship()
    research_paper: Mapped["ResearchPaper"] = relationship()
