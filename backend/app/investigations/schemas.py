"""The API contract for independent investigations.

An investigation is a USER HYPOTHESIS, not market evidence. Nothing in
this module validates that the problem described is real, widespread or
unsolved -- it validates shape only: present, non-blank, bounded. See
app.db.models.investigation.Investigation for why that distinction is
load-bearing.

Pure pydantic. No ORM import, no provider import.
"""

import uuid
from datetime import date, datetime

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
)

from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    InvestigationRunStatus,
    InvestigationStatus,
    ResearchOutcomeReason,
    ResearchQueryStatus,
)
from app.investigations.progress import InvestigationRunPhases
from app.research_intelligence.schemas import (
    ResearchEnrichmentCounters,
    ResearchQueryStateRead,
)
from app.schemas._validators import non_blank

# Longest investigation query GapRadar accepts, in characters.
#
# 2000 is a bounded problem STATEMENT -- roughly a long paragraph -- not
# a pitch deck. The limit exists for two reasons, and the second is the
# one that matters: this text is the input a later phase feeds into query
# generation and semantic matching, where cost and quality both degrade
# with length. An unbounded field would let one request buy an arbitrary
# amount of provider spend.
#
# Applied to the raw submitted string, before whitespace is trimmed: a
# submission that is only enormous because of padding is still enormous
# on the wire.
MAX_QUERY_CHARS = 2000

# Matches Investigation.industry's column width.
MAX_INDUSTRY_CHARS = 255


class InvestigationCreate(BaseModel):
    """What a user submits to start an independent investigation.

    Deliberately does NOT accept title, description or status. Those are
    either derived by GapRadar in a later phase or not yet meaningful,
    and letting a client set them would let it write values the system
    would then read back as if it had produced them.

    UNKNOWN FIELDS ARE REFUSED, not ignored. Silently dropping `title` or
    `status` means a client that believes it set them gets a 201 and a
    body that disagrees with its request, and the mistake surfaces later
    as "GapRadar lost my data". A 422 naming the offending field is the
    only answer that cannot be misread. It also closes the door on a
    future field being added here and quietly accepting values that an
    older deployment ignored.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    industry: str | None = Field(default=None, max_length=MAX_INDUSTRY_CHARS)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        """Trim the edges, keep the wording.

        Outer whitespace is an artefact of typing and pasting, so it is
        removed. Nothing else is: no case folding, no punctuation
        stripping, no inner whitespace collapsing. The user has to be
        able to read their own sentence back verbatim, and a system that
        quietly rewrites the question cannot be trusted about the answer.
        """
        return non_blank(value)

    @field_validator("industry")
    @classmethod
    def industry_not_blank(cls, value: str | None) -> str | None:
        """Absent and blank mean the same thing here, so both become None.

        Storing "" would make "the user did not say" indistinguishable
        from "the user said nothing", and every downstream `if industry`
        would have to know the difference.
        """
        return None if value is None or not value.strip() else non_blank(value)


class InvestigationRead(BaseModel):
    """One persisted investigation, as the frontend sees it.

    Carries no scores and no verdict. Phase 1 stores the investigation
    and nothing about what investigating it found; a zeroed score here
    would be indistinguishable from a real one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    query: str
    title: str | None
    description: str | None
    industry: str | None
    status: InvestigationStatus
    created_at: datetime
    updated_at: datetime


# -- runs -------------------------------------------------------------------
# The execution record behind POST /investigations/{id}/run, as the
# browser sees it. Deliberately reports that work was requested and how it
# ended -- never what it found. What it found is the research
# intelligence, which the client refetches on success rather than reading
# a duplicated summary here that could disagree with it.


class InvestigationRunRead(BaseModel):
    """One investigation run, for polling.

    Shaped like ResearchEnrichmentRead on purpose: it is the same kind of
    object, polled the same way, and a client that already renders one
    should not need a second component to render the other.

    EVERY COUNTER HERE IS FACTUAL AND RESEARCH-ONLY. There is no demand,
    competitor or whitespace progress, because Phase 2 does none of that
    work; a zeroed counter for a phase that never ran would be
    indistinguishable from a phase that ran and found nothing.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    run_id: uuid.UUID = Field(validation_alias=AliasChoices("id"))
    investigation_id: uuid.UUID
    status: InvestigationRunStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Why the run could not be carried out. Present only on FAILED, and
    # written to be shown to a user: never a credential, a stack trace, or
    # a prompt.
    error: str | None = None
    # A run that SUCCEEDED but is incomplete -- some searches returned and
    # some timed out. Set alongside a success, never instead of one.
    warning: str | None = None
    # Per-query progress, in plan order.
    query_states: list[ResearchQueryStateRead] = Field(default_factory=list)
    # WHY this run ended as it did. None for an ordinary success with
    # matches.
    outcome_reason: ResearchOutcomeReason | None = None
    # The RESEARCH funnel, kept because a shipped client contract pins
    # it. Identical to `phases.research`'s four numbers -- both are
    # written from one snapshot by the same helper, so they cannot
    # disagree.
    counters: ResearchEnrichmentCounters = Field(
        default_factory=ResearchEnrichmentCounters
    )
    # PHASE-BY-PHASE PROGRESS. Every number measured, and none of them
    # added together across phases: "18 judged" means nothing without
    # knowing whether it counts papers or web pages. There is no
    # whitespace phase and no verdict, because neither exists.
    phases: InvestigationRunPhases = Field(default_factory=InvestigationRunPhases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def research_queries_total(self) -> int:
        """How many research searches this run's plan called for.

        Zero before a plan exists, which is a fact rather than a
        placeholder: between QUEUED and the first search there genuinely
        is no plan yet.
        """
        return len(self.query_states)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def research_queries_completed(self) -> int:
        """Searches that reached a terminal state, successful or not.

        A timed-out search is COMPLETE from GapRadar's point of view --
        it will contribute nothing more -- even though the provider job
        may still be running. Counting it as pending would leave a
        progress bar stuck forever.
        """
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_retryable(self) -> bool:
        """Whether offering the user a retry could plausibly change this.

        COMPUTED HERE, NOT IN THE BROWSER. The rule is business logic
        about the outcome taxonomy, and a frontend reimplementing it
        would drift the moment a reason is added. A run with no reason is
        not retryable: it either succeeded outright, or it predates the
        taxonomy and guessing would be worse than a missing button.
        """
        return self.outcome_reason is not None and self.outcome_reason.is_retryable

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        return self.status in (
            InvestigationRunStatus.SUCCEEDED,
            InvestigationRunStatus.FAILED,
        )


class InvestigationRunAccepted(BaseModel):
    """The 202 answer to "please investigate this".

    Small on purpose. It reports that work is claimed, not what the work
    found: no paper count, no relevance, because none of that exists yet
    and a placeholder would be indistinguishable from a real result.
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    investigation_id: uuid.UUID
    status: InvestigationRunStatus
    # True when this request joined a run already in flight instead of
    # starting one. Still a 202 -- the caller asked for an investigation
    # and one is happening -- but no second provider run was triggered.
    already_running: bool = False


# -- web evidence read models ----------------------------------------------
# Separate endpoints, separate models. One giant investigation payload
# would make a page that only wants competitors pay for demand evidence,
# and would grow without bound as phases are added.


class WebEvidenceProvenance(BaseModel):
    """How GapRadar came to be looking at one page.

    `found_by_queries` is why hits are kept per search rather than
    collapsed into the evidence row. How many independent search
    directions converged on a page is one of the few honest strength
    signals discovery produces -- and it is reported as the list of
    queries, not as a score, because turning it into one would be the
    fabrication this phase is avoiding.
    """

    model_config = ConfigDict(frozen=True)

    found_by_queries: list[str] = Field(default_factory=list)
    # Best (lowest) rank this page reached in any search that found it.
    # None when no search reported a position.
    best_position: int | None = None


class DemandEvidenceRead(BaseModel):
    """One judged page, as evidence about whether the problem is real."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    url: str
    domain: str
    title: str
    snippet: str
    published_at: date | None = None
    classification: DemandEvidenceClassification
    relevance_score: float
    reason: str
    provenance: WebEvidenceProvenance = Field(
        default_factory=WebEvidenceProvenance
    )


class DemandEvidenceCollection(BaseModel):
    """Everything GapRadar has judged about demand for one investigation.

    DELIBERATELY CARRIES NO DEMAND SCORE. Aggregating classifications
    into a number would require weighing source quality, independence,
    recency and volume, none of which discovery measures. The counts are
    the finding; a score is a later phase that has to earn it.
    """

    model_config = ConfigDict(frozen=True)

    investigation_id: uuid.UUID
    # Counts by classification value, including the ones that do not
    # support the idea. A CONTRADICTS finding is the most valuable thing
    # here and is never hidden.
    counts: dict[str, int] = Field(default_factory=dict)
    evidence: list[DemandEvidenceRead] = Field(default_factory=list)


class CompetitorRead(BaseModel):
    """One judged page, as a competitor to the investigated idea."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    url: str
    domain: str
    # A DISPLAY IDENTITY, not a verified company name: discovery does not
    # open the page, so the page title is what it can honestly report.
    name: str
    snippet: str
    classification: CompetitorClassification
    relevance_score: float
    reason: str
    provenance: WebEvidenceProvenance = Field(
        default_factory=WebEvidenceProvenance
    )


class CompetitorCollection(BaseModel):
    """Everything GapRadar has judged about competition for one investigation.

    No competition score, and no pricing or feature data: discovery reads
    search results, not products.
    """

    model_config = ConfigDict(frozen=True)

    investigation_id: uuid.UUID
    counts: dict[str, int] = Field(default_factory=dict)
    competitors: list[CompetitorRead] = Field(default_factory=list)
