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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

# The only app import here, and deliberately a leaf: app.domain.enums
# defines enums and imports nothing, so this module stays free of the
# ORM and of every provider.
from app.domain.enums import (
    ResearchEnrichmentStatus,
    ResearchOutcomeReason,
    ResearchQueryStatus,
    ResearchSubjectOrigin,
)

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


class ResearchSubject(BaseModel):
    """WHAT the research engine is researching, whatever produced it.

    The generalisation of MarketContext. Query generation, candidate
    pre-filtering and semantic matching all need the same four facts --
    an identity, a stated problem, an elaboration, and optionally an
    industry -- and none of them need those facts to have come from a
    Signal. A user-supplied investigation can supply them just as well,
    and must be able to WITHOUT being written into `signals` first (see
    app.db.models.investigation.Investigation).

    `origin` is not decoration. It is the one thing that survives the
    generalisation: a validated market signal and a sentence a user typed
    are both legitimate research subjects and carry very different
    warrant, and without a label they arrive here as the same anonymous
    triple of strings. Anything that reports on a subject can therefore
    say which kind it was looking at, rather than having to assume.

    Pure: no ORM import, no provider import. Introduced ahead of its
    consumers on purpose -- the research engine still takes MarketContext
    today, and migrating it is a later, separate change. Nothing about
    live opportunity enrichment behaviour depends on this type yet.
    """

    model_config = ConfigDict(frozen=True)

    # Identity of whatever this subject was built from -- a Signal id or
    # an Investigation id. Deliberately NOT called signal_id: the whole
    # point is that it may not be one, and a name that lies about that
    # would send someone straight to the wrong table.
    subject_id: uuid.UUID
    origin: ResearchSubjectOrigin
    # The stated pain, in the words of whoever stated it.
    problem: str
    # The elaboration.
    description: str
    # Genuinely optional: a subject whose author named no industry is not
    # given an invented one.
    industry: str | None = None


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

    def as_research_subject(self) -> ResearchSubject:
        """This market context, as the generic subject contract.

        Total and lossless: every field of MarketContext has exactly one
        counterpart here, `signal_id` becomes `subject_id`, and the
        origin is fixed to SIGNAL because a MarketContext is only ever
        built from a persisted Signal. Nothing is inferred, defaulted or
        dropped -- if that stops being true, the conversion tests fail.
        """
        return ResearchSubject(
            subject_id=self.signal_id,
            origin=ResearchSubjectOrigin.SIGNAL,
            problem=self.problem,
            description=self.description,
            industry=self.industry,
        )


class ResearchQueryPlan(BaseModel):
    """The research searches one SUBJECT should drive, and why.

    `concepts` is the vocabulary the generator actually recognised, which
    is what makes the plan auditable: a plan whose concepts look nothing
    like the problem is a plan whose queries will retrieve the wrong
    literature, and that is visible here before a single provider run is
    paid for.

    `subject_id` was `signal_id` until the research engine was
    generalised. It is the id of the ResearchSubject the plan was built
    for -- a Signal or an Investigation -- and it is REQUIRED, because a
    plan that cannot say what it is a plan for cannot be attributed to
    anything when it is persisted.
    """

    model_config = ConfigDict(frozen=True)

    subject_id: uuid.UUID
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

    SUBJECT-AGNOSTIC BY DESIGN. A frontend, a CLI or an MCP client reads
    the same shape whether the research was found for a trusted market
    Signal or for a user-supplied Investigation, and never has to branch
    on which. `origin` is there for the cases that legitimately care --
    an investigation's research is about a hypothesis nobody has
    corroborated, and a UI may reasonably say so.

    THIS IS THE INTERNAL SHAPE AND THE INVESTIGATION SURFACE'S RESPONSE.
    The opportunity surface serialises OpportunityResearchIntelligence
    instead, which is deliberately frozen at the keys that endpoint
    shipped with -- so a field added here reaches new clients without
    reaching old ones.

    Deliberately absent: research momentum and any composite GapRadar
    score. Neither is computed yet, and shipping a placeholder for them
    would be indistinguishable from a real value.
    """

    model_config = ConfigDict(frozen=True)

    subject_id: uuid.UUID
    origin: ResearchSubjectOrigin
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


class OpportunityResearchIntelligence(BaseModel):
    """The OPPORTUNITY surface's research response. A frozen wire contract.

    Exists solely to stop the generic model's growth from leaking onto an
    endpoint that shipped before that model was generic. When
    ResearchIntelligence gained `subject_id` and `origin`, every consumer
    of GET /opportunities/{id}/research silently started receiving two
    fields it had never been told about -- harmless today, and exactly
    the drift that makes a "read model" quietly become an API.

    So the two are separated. This model names the keys the frontend's
    `ResearchIntelligence` interface actually declares, and nothing else;
    the field list is the contract and is asserted key-by-key by
    tests/api/test_opportunity_research.py.

    NOT A SECOND READ MODEL. It computes nothing, queries nothing, and
    reorders nothing -- `from_intelligence` copies fields off the value
    the shared engine already produced. Adding a field to the generic
    model does NOT add it here, which is the entire point.
    """

    model_config = ConfigDict(frozen=True)

    # Required and non-null, as it was before the generalisation: this
    # endpoint is only ever reached for a Signal.
    signal_id: uuid.UUID
    generated_queries: list[str] = Field(default_factory=list)
    paper_count: int = 0
    matched_paper_count: int = 0
    average_relevance_score: float | None = None
    top_concepts: list[str] = Field(default_factory=list)
    top_papers: list[ResearchPaperMatch] = Field(default_factory=list)

    @classmethod
    def from_intelligence(
        cls, intelligence: "ResearchIntelligence"
    ) -> "OpportunityResearchIntelligence":
        """Narrow the shared read model onto this endpoint's contract.

        Refuses an investigation's research outright rather than
        rendering it with a `signal_id` that would be a lie about where
        the research came from. Unreachable through the route, which
        resolves the signal before asking -- so reaching it means a
        caller wired the wrong subject, and failing loudly beats
        publishing mislabelled provenance.
        """
        if intelligence.origin is not ResearchSubjectOrigin.SIGNAL:
            raise ValueError(
                "the opportunity research contract requires a SIGNAL subject; "
                f"got {intelligence.origin.value}"
            )
        return cls(
            signal_id=intelligence.subject_id,
            generated_queries=list(intelligence.generated_queries),
            paper_count=intelligence.paper_count,
            matched_paper_count=intelligence.matched_paper_count,
            average_relevance_score=intelligence.average_relevance_score,
            top_concepts=list(intelligence.top_concepts),
            top_papers=list(intelligence.top_papers),
        )


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


class ResearchEnrichmentCounters(BaseModel):
    """One run's funnel, as four numbers that mean four different things.

        discovered -> distinct papers acquired across all searches
        selected   -> survivors of the pre-filter and the candidate cap
        judged     -> papers the semantic matcher actually returned on
        matched    -> papers at or above the relevance threshold

    They narrow monotonically and must never be substituted for one
    another. Reporting `discovered` where `judged` belongs is precisely
    how a run showed "34 papers" under semantic matching while its own
    summary said 20 -- and `discovered` is a DISTINCT count, never the
    sum of per-query totals, because a paper found by two searches is one
    paper.

    Every field defaults to 0 so a row written before counters existed
    (`counters == {}`) deserializes rather than 500ing. Zeros there mean
    "not recorded", which the client distinguishes by the run predating
    the feature, not by the numbers themselves.
    """

    model_config = ConfigDict(frozen=True)

    discovered: int = 0
    selected: int = 0
    judged: int = 0
    matched: int = 0


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
    # WHY this run ended as it did. None for an ordinary success with
    # matches, and for rows written before outcome reasons existed.
    outcome_reason: ResearchOutcomeReason | None = None
    counters: ResearchEnrichmentCounters = Field(
        default_factory=ResearchEnrichmentCounters
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_retryable(self) -> bool:
        """Whether offering the user a retry could plausibly change this.

        COMPUTED HERE, NOT IN THE BROWSER. The rule is business logic
        about the outcome taxonomy, and a frontend reimplementing it
        would drift the moment a reason is added. A run with no reason is
        not retryable: either it succeeded outright, or it predates the
        taxonomy and guessing would be worse than a missing button.
        """
        return self.outcome_reason is not None and self.outcome_reason.is_retryable

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_success(self) -> bool:
        """Whether this run produced a usable answer.

        Derived from `status` first: SUCCEEDED is the authority, and the
        reason only adds nuance to it. A zero-match run and a partial
        acquisition are both successes -- rendering either as a failure
        is the bug this field exists to prevent.
        """
        return self.status is ResearchEnrichmentStatus.SUCCEEDED

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
