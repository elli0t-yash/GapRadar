"""Web discovery persistence: what was searched, what came back, what it means.

THREE TABLES, THREE DIFFERENT KINDS OF FACT, deliberately not merged:

    InvestigationWebSearchRun   one provider execution (query + locale +
                                latency + status). Observability.
    InvestigationWebSearchHit   one URL as returned by ONE search, at one
                                rank. Provenance.
    InvestigationDemandEvidence one JUDGEMENT about one URL for one
    InvestigationCompetitor     investigation. Semantics.

The split is what makes "how many search directions found this page" an
answerable question. A single wide table would have to choose between
duplicating the verdict per query (so re-judging updates some rows and
not others) or discarding which query found it (so convergence, one of
the few honest strength signals discovery produces, is lost).

Hits carry the URL rather than a foreign key to an evidence row. That
avoids a polymorphic reference -- a hit may belong to a demand search, a
competitor search, or both -- while keeping the join trivial: hits and
evidence meet on (investigation, url), and the search run says which
family asked.
"""

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    WebSearchStatus,
)
from app.web_intelligence.schemas import WebSearchFamily

# The families a persisted search run may name, as a SQL predicate.
#
# LOWERCASE, AND THAT IS NOT AN OVERSIGHT. `family` is a plain VARCHAR
# written from `WebSearchFamily.value` -- unlike the status columns
# nearby, which are SQLAlchemy Enums and therefore persist the member
# NAME in uppercase. A predicate written in the status columns' casing
# would match no row here and leave a constraint that is present, valid,
# and rejects everything.
#
# Derived from the enum rather than typed out, and SORTED so that
# reordering the enum's members cannot change the string. The migration
# spells the same predicate literally; a test pins the two together.
WEB_SEARCH_FAMILY_PREDICATE = "family IN ({})".format(
    ", ".join(f"'{value}'" for value in sorted(f.value for f in WebSearchFamily))
)

if TYPE_CHECKING:
    from app.db.models.investigation import Investigation
    from app.db.models.investigation_run import InvestigationRun


class InvestigationWebSearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ONE provider execution. Observability, never evidence.

    Exists so a run can be explained after the fact: which query was
    submitted, from which locale, how long the provider took, what it
    returned, and whether it worked. None of that belongs on an evidence
    row -- a page's credibility does not depend on how slow the search
    that found it was -- and keeping it here is what lets the evidence
    tables stay purely semantic.

    `family` is the question that was asked, decided before the provider
    was contacted. It is what makes "2 of 3 demand searches complete" a
    fact rather than a guess.

    `provider_request_id` is NULLABLE and stays null unless the provider
    supplies one. The synchronous SERP API does not, and inventing an id
    to fill the column would make an untraceable request look traceable.
    """

    __tablename__ = "investigation_web_search_runs"
    __table_args__ = (
        # A search run names one of the families the engine actually
        # runs. Enforced by the database, not only by the orchestration:
        # `family` is a bare string column, so without this a typo or a
        # future writer could persist a row that every family-scoped
        # read silently skips -- a paid provider request that no phase
        # counts and no evidence hangs off.
        CheckConstraint(
            WEB_SEARCH_FAMILY_PREDICATE,
            name="ck_investigation_web_search_runs_family",
        ),
        Index(
            "ix_investigation_web_search_runs_investigation_id", "investigation_id"
        ),
        Index("ix_investigation_web_search_runs_run_id", "investigation_run_id"),
        Index("ix_investigation_web_search_runs_family", "family"),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id"), nullable=False
    )
    # WHICH ATTEMPT ran this search. Nullable because a search could be
    # replayed or backfilled outside a run, and refusing to record that
    # would push it somewhere with no provenance at all.
    investigation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_runs.id")
    )
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    # The query as submitted, verbatim. Normalizing it here would make
    # two genuinely different searches look like one.
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    # The provider's product, e.g. "serp_api". Recorded so a future
    # provider swap is visible in the data, not only in git history.
    product: Mapped[str] = mapped_column(String(64), nullable=False)
    locale_country: Mapped[str] = mapped_column(String(2), nullable=False)
    locale_language: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[WebSearchStatus] = mapped_column(
        Enum(WebSearchStatus, name="web_search_status", native_enum=False, length=32),
        nullable=False,
    )
    # How many usable records this search produced. 0 on a SUCCEEDED row
    # means the engine looked and found nothing; on a FAILED row it means
    # nothing was learned. The status is what separates them.
    records_returned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Present only on failure, written to be shown to a user: never a
    # credential, a stack trace, or a provider payload.
    error: Mapped[str | None] = mapped_column(Text)
    # The provider's own request id, when it issues one. An identifier,
    # never a credential. Null is the honest value for a synchronous
    # request that returns none.
    provider_request_id: Mapped[str | None] = mapped_column(String(255))

    investigation: Mapped["Investigation"] = relationship()
    run: Mapped["InvestigationRun | None"] = relationship()
    hits: Mapped[list["InvestigationWebSearchHit"]] = relationship(
        back_populates="search_run"
    )


class InvestigationWebSearchHit(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One URL, as returned by ONE search, at one rank.

    THE PROVENANCE ROW. Hits only ever accumulate; they are never
    updated, because "this search returned this page at position 3" is a
    fact about a moment and re-running does not revise it -- it produces
    a new search run with new hits.

    Carries the page's surface text as it was at discovery. The evidence
    tables carry it too, and that is not duplication by accident: the hit
    is what the provider said then, and the evidence row is GapRadar's
    current view of the page. When a later search returns a changed
    title, the difference between the two is visible instead of
    overwritten.
    """

    __tablename__ = "investigation_web_search_hits"
    __table_args__ = (
        # A URL appears at most once per search -- within-query dedupe is
        # applied during normalization, and this is the database refusing
        # to let a second writer skip it.
        UniqueConstraint(
            "investigation_web_search_run_id",
            "url",
            name="uq_investigation_web_search_hits_run_url",
        ),
        Index(
            "ix_investigation_web_search_hits_run_id",
            "investigation_web_search_run_id",
        ),
        # The lookup that answers "which searches found this page?"
        Index("ix_investigation_web_search_hits_url", "url"),
    )

    investigation_web_search_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigation_web_search_runs.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The provider's rank on page 0. Null when it did not say -- never 0,
    # which would be a real first position.
    position: Mapped[int | None] = mapped_column(Integer)
    # A calendar DATE, and only when the provider stated a reliable
    # absolute one. Never inferred.
    published_at: Mapped[date | None] = mapped_column(Date)

    search_run: Mapped["InvestigationWebSearchRun"] = relationship(
        back_populates="hits"
    )


class InvestigationDemandEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One judgement about whether a page evidences the stated problem.

    ONE ROW PER (investigation, url), whichever queries found it. A page
    discovered by three different demand searches is one piece of
    evidence with three provenance trails, not three pieces of evidence:
    counting it three times would let a well-indexed blog post look like
    a market.

    Deliberately absent: any demand score. Aggregating five
    classifications into a number would require weighing source quality,
    independence, recency and volume, none of which GapRadar measures.
    The classifications are the finding; the score is a later phase that
    has to earn it.
    """

    __tablename__ = "investigation_demand_evidence"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "url",
            name="uq_investigation_demand_evidence_investigation_url",
        ),
        Index(
            "ix_investigation_demand_evidence_investigation_id", "investigation_id"
        ),
        Index(
            "ix_investigation_demand_evidence_classification",
            "investigation_id",
            "classification",
        ),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[date | None] = mapped_column(Date)
    classification: Mapped[DemandEvidenceClassification] = mapped_column(
        Enum(
            DemandEvidenceClassification,
            name="demand_evidence_classification",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    # NOT NULL. A verdict with no strength is an assertion with no
    # evidence, and this project does not store those.
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    # Why this page counts as evidence about THIS problem. Never a
    # summary of the page.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    investigation: Mapped["Investigation"] = relationship()


class InvestigationCompetitor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One judgement about how a discovered page relates to the idea.

    ONE ROW PER (investigation, url), for the same reason as demand
    evidence.

    `name` IS A DISPLAY IDENTITY, NOT A VERIFIED COMPANY NAME. Extracting
    a reliable vendor name from a SERP title and snippet is not something
    discovery can do, so the page title is used and is labelled as such.
    A confidently wrong company name would be worse than an honest title,
    and a later deep-read phase is where a real name can be established.

    No pricing, no feature list, no funding. Discovery does not open the
    page, so it has no basis for any of them.
    """

    __tablename__ = "investigation_competitors"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "url",
            name="uq_investigation_competitors_investigation_url",
        ),
        Index("ix_investigation_competitors_investigation_id", "investigation_id"),
        Index(
            "ix_investigation_competitors_classification",
            "investigation_id",
            "classification",
        ),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    # The page title unless something better is genuinely available.
    name: Mapped[str] = mapped_column(String(1024), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    classification: Mapped[CompetitorClassification] = mapped_column(
        Enum(
            CompetitorClassification,
            name="competitor_classification",
            native_enum=False,
            length=32,
        ),
        nullable=False,
    )
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    investigation: Mapped["Investigation"] = relationship()
