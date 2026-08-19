"""Contracts for the research side: what comes in, what is stored, what happened.

The input contract is the validated Bright Data arXiv collector output
(`external/brightdata/arxiv/schema.json`). Acquisition is not GapRadar's
concern -- something else fetches the records and hands the list over --
so nothing in this module knows about HTTP, Bright Data, or credentials.
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# The only app import here, and deliberately a leaf: app.domain.enums
# defines enums and imports nothing, so this module stays free of the
# ORM and of every provider.
from app.domain.enums import ResearchEnrichmentStatus, ResearchQueryStatus

# One raw record exactly as the arXiv collector delivers it. Left as a
# plain dict rather than a strict model on purpose: it is UNTRUSTED
# provider output, and a pydantic model here would raise ValidationError
# for a bad field instead of producing the reason-coded RejectedRecord
# this layer promises. app.research_intelligence.normalizer is the single
# place that decides what a valid record is.
RawResearchRecord = dict[str, Any]


class ResearchRejectionReason(str, enum.Enum):
    """Why one record could not become a ResearchPaper.

    Mirrors app.ingestion.schemas.RejectionReason in shape and intent:
    stable codes so a caller can count and group failures without parsing
    English.
    """

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_ARXIV_ID = "invalid_arxiv_id"
    INVALID_URL = "invalid_url"
    INVALID_PUBLICATION_DATE = "invalid_publication_date"
    INVALID_AUTHORS = "invalid_authors"
    INVALID_CATEGORIES = "invalid_categories"
    INVALID_RECORD = "invalid_record"


class ResearchCategory(BaseModel):
    """One arXiv subject category, split into its two halves.

    arXiv publishes "Systems and Control (eess.SY)". The code is what a
    matcher filters on; the label is what a person reads. `code` is None
    when the source emitted something with no parenthesised code -- the
    text is kept as a label rather than discarded, because the source
    owns its own vocabulary and an unfamiliar shape is not a defect.
    """

    model_config = ConfigDict(frozen=True)

    code: str | None = None
    label: str


class MarketContext(BaseModel):
    """The market pain, as the research side needs to see it.

    Built from a persisted Signal via
    app.research_intelligence.service.market_context_from_signal, which
    routes through the Opportunity read model so query generation and
    matching see exactly the wording the product surface shows -- not a
    second, quietly divergent reading of the same row.

    Pure: no ORM import, so every consumer of this module stays free of
    the database.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: uuid.UUID
    # Signal.title / Opportunity.problem -- the stated pain.
    problem: str
    # Signal.body / Opportunity.description -- the elaboration.
    description: str
    # Source-published, and genuinely optional: a signal whose metadata
    # carries no industry is not given an invented one.
    industry: str | None = None


class ResearchQueryPlan(BaseModel):
    """The research searches one opportunity should drive, and why.

    `concepts` is the vocabulary the generator actually recognised, which
    is what makes the plan auditable: a plan whose concepts look nothing
    like the problem is a plan whose queries will retrieve the wrong
    literature, and that is visible here before a single provider run is
    paid for.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: uuid.UUID
    queries: list[str]
    concepts: list[str] = Field(default_factory=list)
    rationale: str = ""


class NormalizedResearchPaper(BaseModel):
    """A validated, normalized paper, ready to construct a ResearchPaper row.

    Deliberately carries no `query`: a paper is an entity, and the query
    that happened to surface it is provenance about a search, not a
    property of the paper. See app.db.models.research_paper.
    """

    model_config = ConfigDict(frozen=True)

    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[ResearchCategory]
    published_at: date
    paper_url: str
    pdf_url: str

    @property
    def primary_category_code(self) -> str | None:
        """The first category's code, or None if it has none."""
        return self.categories[0].code if self.categories else None

    def category_payload(self) -> list[dict[str, Any]]:
        """Categories as the JSON shape the ResearchPaper column stores."""
        return [category.model_dump(mode="json") for category in self.categories]


class RejectedResearchRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    reason: ResearchRejectionReason
    detail: str
    # The record as received, preserved for debugging. Untrusted.
    raw: RawResearchRecord


class ResearchIngestionResult(BaseModel):
    """What one ingestion call did.

    `created` / `updated` / `unchanged` partition the records that
    normalized successfully and were not in-batch duplicates. The split
    matters: re-ingesting an identical batch must report `unchanged`, not
    `updated`, or idempotency is unobservable.
    """

    model_config = ConfigDict(frozen=True)

    search_run_id: uuid.UUID
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # The same arxiv_id appearing twice inside ONE batch. Not a count of
    # papers already in the database -- those are `unchanged`/`updated`.
    duplicates_in_batch: int = 0
    rejected: list[RejectedResearchRecord] = Field(default_factory=list)
    # Every paper this search resolved to, in the order returned.
    research_paper_ids: list[uuid.UUID] = Field(default_factory=list)

    @property
    def accepted(self) -> int:
        """Records that became a paper this call, new or existing."""
        return self.created + self.updated + self.unchanged


# -- read model -------------------------------------------------------------
# The frontend-facing view of one opportunity's research intelligence.
# Read-only and computed from persisted rows: nothing here searches,
# judges, or acquires anything.


class ResearchPaperMatch(BaseModel):
    """One matched paper, with the verdict that admitted it."""

    model_config = ConfigDict(frozen=True)

    research_paper_id: uuid.UUID
    arxiv_id: str
    title: str
    # The full abstract, for a detail view.
    abstract: str
    # A card-sized excerpt, cut at a word boundary. Provided alongside
    # the full text rather than instead of it so a list of ten papers is
    # not ~16 KB of prose the caller has to truncate itself.
    abstract_preview: str
    authors: list[str] = Field(default_factory=list)
    categories: list[dict[str, Any]] = Field(default_factory=list)
    published_at: date
    paper_url: str
    pdf_url: str
    # 0-100, same scale as opportunity_score on the market side.
    relevance_score: float
    matched_concepts: list[str] = Field(default_factory=list)
    match_reason: str | None = None
    # None means "not assessed", never "not ready".
    technical_readiness_score: float | None = None


class ResearchIntelligence(BaseModel):
    """Everything GapRadar currently knows about the research behind one pain.

    Deliberately absent: research momentum and any composite GapRadar
    score. Neither is computed yet, and shipping a placeholder for them
    would be indistinguishable from a real value.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: uuid.UUID
    # Distinct queries this opportunity has actually been searched with,
    # most recently searched first. Empty means no enrichment has run.
    generated_queries: list[str] = Field(default_factory=list)
    # Distinct papers any of those searches returned -- the candidate
    # pool, not the accepted set.
    paper_count: int = 0
    # Papers that passed the relevance threshold.
    matched_paper_count: int = 0
    # Mean relevance across matches. None -- never 0 -- when there are no
    # matches: an average of nothing is not zero.
    average_relevance_score: float | None = None
    # Concepts that recur across the matches, most frequent first.
    top_concepts: list[str] = Field(default_factory=list)
    top_papers: list[ResearchPaperMatch] = Field(default_factory=list)


# -- on-demand enrichment ---------------------------------------------------
# The job record behind POST /research/enrich, as the browser sees it.
# Deliberately reports that work was requested and how it ended -- never
# what it found. What it found is the research intelligence itself, which
# the client refetches on success rather than reading a duplicated summary
# here that could disagree with it.


class ResearchQueryStateRead(BaseModel):
    """One research query's observable state, for polling.

    Exists so the browser can say "2 of 3 searches complete" from FACTS.
    Without it the UI would have to animate stage progress on a timer,
    which is exactly how a 14-minute run came to look identical to a
    working one.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    status: ResearchQueryStatus
    # Bright Data's own job id ("j_..."). An identifier, never a
    # credential -- it is what makes a slow search traceable in Scraper
    # Studio without reading the backend logs.
    provider_job_id: str | None = None
    records_received: int = 0
    papers_returned: int = 0
    error: str | None = None
    elapsed_seconds: float | None = None


class ResearchEnrichmentRead(BaseModel):
    """One enrichment job, for polling."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    enrichment_id: uuid.UUID = Field(validation_alias=AliasChoices("id"))
    signal_id: uuid.UUID
    status: ResearchEnrichmentStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Why the job could not be carried out. Present only on FAILED, and
    # written to be shown to a user: never a credential, a stack trace, or
    # a prompt.
    error: str | None = None
    # A run that SUCCEEDED but is incomplete -- some searches returned and
    # some timed out. Set alongside a success, never instead of one: the
    # research that was found is real, and the gap is stated rather than
    # hidden.
    warning: str | None = None
    # Per-query progress, in plan order. Empty for runs that predate
    # per-query tracking, which is why the client must treat it as
    # optional detail rather than the source of overall status.
    query_states: list[ResearchQueryStateRead] = Field(default_factory=list)

    @property
    def searches_total(self) -> int:
        return len(self.query_states)

    @property
    def searches_complete(self) -> int:
        return sum(
            1
            for state in self.query_states
            if state.status
            in (
                ResearchQueryStatus.SUCCEEDED,
                ResearchQueryStatus.FAILED,
                ResearchQueryStatus.TIMED_OUT,
            )
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ResearchEnrichmentStatus.SUCCEEDED,
            ResearchEnrichmentStatus.FAILED,
        )


class ResearchEnrichmentAccepted(BaseModel):
    """The 202 answer to "please analyse research for this opportunity".

    Small on purpose. It reports that work is claimed, not what the work
    found: no paper count, no relevance, because none of that exists yet
    and a placeholder would be indistinguishable from a real result.
    """

    model_config = ConfigDict(frozen=True)

    enrichment_id: uuid.UUID
    signal_id: uuid.UUID
    status: ResearchEnrichmentStatus
    # True when this request joined a job already in flight instead of
    # starting one. Still a 202 -- the caller asked for analysis and
    # analysis is happening -- but no second provider run was triggered.
    already_running: bool = False
    # True when this opportunity already has persisted research and no new
    # job was started. The client should simply read GET /research.
    already_enriched: bool = False
