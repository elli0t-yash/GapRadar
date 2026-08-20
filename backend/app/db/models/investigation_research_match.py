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
    from app.db.models.investigation import Investigation
    from app.db.models.research_paper import ResearchPaper


class InvestigationResearchMatch(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One claim that a paper is relevant to one user-supplied investigation.

    The twin of OpportunityResearchMatch, and deliberately a SEPARATE
    TABLE rather than a shared one with a nullable signal_id/
    investigation_id pair or a polymorphic subject_id:

    - Both sides keep a real foreign key. A polymorphic id cannot be
      enforced by any database, so an investigation match pointing at a
      deleted subject would become possible the day the abstraction was
      introduced.
    - The uniqueness rule is per subject kind. `(investigation_id,
      research_paper_id)` and `(signal_id, research_paper_id)` are
      different constraints over different columns, and expressing both
      through one nullable pair means neither is enforced when the other
      column is null.
    - MOST IMPORTANTLY: a verdict about a user hypothesis and a verdict
      about validated market evidence must not be able to overwrite one
      another. The same paper judged for a Signal and for an
      Investigation is two independent claims, made against two different
      problem statements, and they coexist by construction here.

    RESEARCH PAPERS ARE NOT DUPLICATED. `research_papers` stays globally
    unique on arxiv_id -- a paper is an entity and is stored once no
    matter which kind of subject found it. Only the JUDGEMENT is
    per-subject, which is exactly what this table holds.
    """

    __tablename__ = "investigation_research_matches"
    __table_args__ = (
        # One verdict per (investigation, paper). Re-running an
        # investigation updates the existing row instead of stacking
        # near-duplicate claims.
        UniqueConstraint(
            "investigation_id",
            "research_paper_id",
            name="uq_investigation_research_matches_investigation_paper",
        ),
        Index(
            "ix_investigation_research_matches_investigation_id", "investigation_id"
        ),
        Index("ix_investigation_research_matches_paper_id", "research_paper_id"),
        # Ranking a single investigation's matches best-first.
        Index(
            "ix_investigation_research_matches_relevance",
            "investigation_id",
            "relevance_score",
        ),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id"), nullable=False
    )
    research_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_papers.id"), nullable=False
    )
    # NOT NULL on purpose. A match with no relevance score is an
    # assertion with no evidence, and this project does not store those.
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    # Null means "not assessed", never "not ready".
    technical_readiness_score: Mapped[float | None] = mapped_column(Float)
    matched_concepts: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    # Human-readable justification. Nullable: a purely numeric matcher has
    # no sentence to offer, and an invented one would be worse than none.
    match_reason: Mapped[str | None] = mapped_column(Text)

    investigation: Mapped["Investigation"] = relationship()
    research_paper: Mapped["ResearchPaper"] = relationship()
