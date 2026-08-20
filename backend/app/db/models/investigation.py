from sqlalchemy import Enum, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import InvestigationStatus


class Investigation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-supplied idea, problem or hypothesis to investigate.

    PROVENANCE IS THE REASON THIS TABLE EXISTS SEPARATELY FROM `signals`.

    A Signal is externally discovered market evidence: something a source
    published, that a collector fetched, that satisfied that source's
    contract, and that RecallGuard is willing to call trustworthy. An
    Investigation is a sentence a user typed. It has been validated for
    shape -- non-blank, bounded -- and for nothing else. Nobody has
    corroborated that the problem is real, widespread, or unsolved.

    Storing an investigation as a Signal would erase that difference
    permanently: every downstream consumer that reads `signals` does so
    on the understanding that the row survived collection and validation,
    and a user hypothesis inserted there would inherit trust it never
    earned. So investigations live here, and the research engine is given
    an explicitly-labelled subject (app.research_intelligence.schemas
    .ResearchSubject) rather than being handed a fake signal.

    Every row in this table is user-supplied by construction -- there is
    no other writer -- which is why there is no origin column: a column
    with one possible value records nothing.

    Deliberately thin. It stores the investigation itself and nothing
    about what investigating it found: no demand score, no research
    score, no competitor or whitespace assessment, no verdict. None of
    those values exist yet, and a column of nulls is indistinguishable
    from a real result that happens to be missing.
    """

    __tablename__ = "investigations"
    __table_args__ = (
        # Recent-first listing is the only read path this table has, and
        # the tie-break column is the primary key, so the index that
        # serves it is the one on created_at.
        Index("ix_investigations_created_at", "created_at"),
        Index("ix_investigations_status", "status"),
    )

    # The user's wording, preserved. Trimmed of outer whitespace and
    # bounded in length by the API schema; never rewritten, expanded or
    # "improved" on the way in. A later phase may DERIVE a normalized
    # query from this, but it will be a separate value: what the user
    # asked has to stay readable back to them verbatim.
    query: Mapped[str] = mapped_column(Text, nullable=False)
    # Human-facing summaries of the investigation. Nullable because
    # nothing produces them yet -- the create API does not accept them,
    # and inventing them from the query would be fabrication.
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # Optional, and genuinely optional: an investigation whose author did
    # not name an industry is not given an invented one. Mirrors the same
    # decision on MarketContext.industry.
    industry: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[InvestigationStatus] = mapped_column(
        Enum(
            InvestigationStatus,
            name="investigation_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Investigation id={self.id} status={self.status.value}>"
