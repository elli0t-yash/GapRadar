from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Date, Enum, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import ResearchSource

if TYPE_CHECKING:
    from app.db.models.research_search import ResearchSearchResult


class ResearchPaper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One research paper, stored once no matter how often it is found.

    THE PAPER IS AN ENTITY, NOT AN OBSERVATION. That distinction drives
    the whole design:

    - `query` is deliberately NOT a column here. The same paper is
      legitimately returned by many different searches, and stamping one
      query onto the paper would make the fifth search overwrite the
      first, or force a duplicate row per query. Which queries found this
      paper lives in ResearchSearchRun/ResearchSearchResult, where it can
      be many.
    - A paper may match many Opportunities, and an Opportunity may match
      many papers. That lives in OpportunityResearchMatch. Nothing about
      matching is stored here.

    Identity is `arxiv_id`, which is why it is UNIQUE. The normalizer
    strips any trailing version suffix before it gets here, so
    "2608.13083", "2608.13083v1" and "2608.13083v3" all resolve to one
    row -- they are revisions of one paper, not three papers.

    `source` records which research source produced the row. Today it is
    always ARXIV and the unique constraint is on `arxiv_id` alone; a
    second source with its own id namespace would need a deliberate
    migration to a composite key, which is the point of keeping the
    column rather than hardcoding the assumption.

    No raw provider payload is stored. Unlike Signal.metadata -- which
    carries source-published values GapRadar does not otherwise model --
    every field the arXiv contract delivers is modelled here, so a
    verbatim copy would duplicate ~2 KB of abstract per row and, because
    rows are upserted across many searches, would leave it ambiguous
    which search's copy won. Per-observation audit belongs on the search
    result row, not on the paper.
    """

    __tablename__ = "research_papers"
    __table_args__ = (
        # Identity. One row per paper, forever.
        UniqueConstraint("arxiv_id", name="uq_research_papers_arxiv_id"),
        Index("ix_research_papers_published_at", "published_at"),
        Index("ix_research_papers_primary_category_code", "primary_category_code"),
        Index("ix_research_papers_source", "source"),
    )

    arxiv_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[ResearchSource] = mapped_column(
        Enum(ResearchSource, name="research_source", native_enum=False, length=32),
        nullable=False,
        default=ResearchSource.ARXIV,
    )
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    # An ordered list of normalized author names. JSON rather than a join
    # table because these are opaque value strings: nothing queries by
    # author, nothing joins on one, and an author has no identity of its
    # own in this system. Promoting them to rows would add two tables and
    # a resolution problem ("is this the same J. Smith?") that GapRadar
    # has no way to answer.
    authors: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    # Normalized objects: [{"code": "eess.SY", "label": "Systems and Control"}].
    # arXiv publishes "Systems and Control (eess.SY)"; both halves are
    # kept because the code is what a matcher filters on and the label is
    # what a UI shows. `code` may be null if a future category does not
    # carry one -- the source owns its own vocabulary, so an unparseable
    # category is preserved as a label rather than rejected.
    categories: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    # The first category's code, lifted out as a real indexed column so
    # "papers in eess.SY" is a plain WHERE rather than JSON traversal.
    # Denormalized from `categories` on purpose; the normalizer is the
    # only writer of both.
    primary_category_code: Mapped[str | None] = mapped_column(String(64))
    # A calendar DATE, not a timestamp. arXiv publishes "2026-08-13" with
    # no time and no timezone, and this codebase does not invent either
    # (app.ingestion.normalizer.parse_timestamp rejects naive timestamps
    # rather than assuming UTC). Storing a timestamptz would require
    # guessing both.
    published_at: Mapped[date] = mapped_column(Date, nullable=False)
    paper_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    pdf_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    search_results: Mapped[list["ResearchSearchResult"]] = relationship(
        back_populates="research_paper"
    )
