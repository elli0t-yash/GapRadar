import enum


class SourceType(str, enum.Enum):
    WEB = "web"
    FORUM = "forum"
    SOCIAL = "social"
    REVIEW = "review"
    OTHER = "other"


class CollectorStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PipelineRunStatus(str, enum.Enum):
    """Lifecycle of ONE logical pipeline execution, persisted.

    Deliberately separate from RunStatus and from ReliabilityState:

    - RunStatus describes one provider collection execution. A logical
      pipeline execution can span several of them (a detection run and
      the independent verification run that proves a repair).
    - ReliabilityState describes whether a collector's DATA can be
      trusted. A pipeline execution sitting in WAITING_PROVIDER says
      nothing about the trust of the dataset already collected: the
      collector can be HEALTHY and a refresh can be mid-flight at the
      same time, and conflating the two would make a refresh look like a
      degradation.

    QUEUED..VERIFYING are active; COMPLETED, DEGRADED and FAILED are
    terminal.

    DEGRADED and FAILED are different facts. DEGRADED means the execution
    ran to completion and RecallGuard judged the result untrustworthy --
    the system working. FAILED means the execution could not be carried
    out at all (GapRadar crashed, or the provider job could not be
    reached), which is an operational problem, not a trust verdict.
    """

    QUEUED = "queued"
    COLLECTING = "collecting"
    WAITING_PROVIDER = "waiting_provider"
    VALIDATING = "validating"
    INGESTING = "ingesting"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"


class ResearchSource(str, enum.Enum):
    """Where a ResearchPaper came from.

    One member today. It exists rather than a bare string so the value is
    spelled in exactly one place and a second research source is a
    deliberate, reviewable addition instead of a typo that silently
    creates a parallel namespace.
    """

    ARXIV = "arxiv"


class ResearchEnrichmentStatus(str, enum.Enum):
    """Lifecycle of ONE on-demand research enrichment, persisted.

    Deliberately coarse. Unlike PipelineRunStatus it does not name the
    internal stage, because the frontend must not claim a stage the
    backend cannot actually prove it is in: the orchestration runs three
    provider searches and a judging pass without reporting which one is
    live. Four states is what the backend can honestly say.

    QUEUED and RUNNING are active; SUCCEEDED and FAILED are terminal.

    SUCCEEDED means the enrichment ran to completion -- NOT that it found
    anything. A run that searched honestly and matched nothing is a
    success whose result is zero matches, which the research read model
    already distinguishes from "never enriched".
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SignalType(str, enum.Enum):
    COMPLAINT = "complaint"
    QUESTION = "question"
    FEATURE_REQUEST = "feature_request"
    REVIEW = "review"
    # GapRadar's two first-class signal roles: a stated unsolved problem
    # (e.g. Razorpay's Fix My Itch) and published research (e.g. arXiv).
    PROBLEM = "problem"
    RESEARCH = "research"
    OTHER = "other"


class IncidentStatus(str, enum.Enum):
    """Lifecycle of one reliability incident.

    Deliberately has no HEALTHY member: "healthy" is the absence of an
    active incident, not an incident state, so a healthy collector never
    needs a row here. RECOVERED is terminal and historical -- it records
    that this incident was proven repaired, not that the collector is
    currently fine (see app.recallguard.service.collector_reliability_state).
    """

    DEGRADED = "degraded"
    HEALING = "healing"
    VALIDATING = "validating"
    VERIFYING = "verifying"
    RECOVERED = "recovered"
    MANUAL_REVIEW = "manual_review"


class FailureClassification(str, enum.Enum):
    """What kind of failure an incident represents.

    OUTAGE: the provider could not execute the collection (trigger
    failure, provider error, local timeout). The scraper itself is not
    implicated.

    EXTRACTION_DRIFT: the collection ran but what came back is wrong --
    a malformed payload, records violating the source contract, or a
    collapse in completeness. This is the condition a scraper repair can
    plausibly address.

    SOURCE_ABSENCE: the source itself genuinely changed or removed the
    data. Never inferred automatically; it requires deliberate
    classification, because "the scraper broke" and "the data is gone"
    look identical from an empty dataset.

    UNKNOWN: anything else, including GapRadar's own ingestion or
    database failures, which must never be blamed on the scraper.
    """

    OUTAGE = "outage"
    EXTRACTION_DRIFT = "extraction_drift"
    SOURCE_ABSENCE = "source_absence"
    UNKNOWN = "unknown"


class RecommendedAction(str, enum.Enum):
    """What RecallGuard advises. It never performs the action itself."""

    RETRY = "retry"
    REQUEST_HEAL = "request_heal"
    ACCEPT_SOURCE_CHANGE = "accept_source_change"
    INVESTIGATE = "investigate"
    ESCALATE = "escalate"


class ResearchOutcomeReason(str, enum.Enum):
    """WHY a research enrichment ended the way it did, persisted.

    Exists so the frontend never has to parse a human error string to
    decide what to render. "this problem's wording matched no known
    research vocabulary" is a sentence for a person; RETRY-ability is a
    decision, and decisions must come from a typed value.

    Only ever set alongside a terminal status, and only when there is
    something to say: an ordinary success with matches carries None.

    The retryable/non-retryable split is the point. Offering "Try again"
    for a deterministic rejection produces the identical result and
    teaches users the button is a lie.
    """

    # -- terminal successes with something worth explaining -------------
    # Every search ran, papers were judged, none crossed the threshold.
    # A real answer, not a failure.
    NO_RELEVANT_RESEARCH = "no_relevant_research"
    # Some searches returned and some did not; the result is real but
    # built on less than the full plan.
    ACQUISITION_PARTIAL = "acquisition_partial"

    # -- terminal failures the user CANNOT fix by retrying --------------
    # Neither the deterministic generator nor the LLM fallback could
    # produce queries specific enough to be worth a provider call.
    # Retrying the same problem text produces the same answer.
    QUERY_PLAN_UNAVAILABLE = "query_plan_unavailable"
    # The opportunity disappeared underneath the run.
    OPPORTUNITY_MISSING = "opportunity_missing"

    # -- terminal failures where retrying can genuinely differ ----------
    # The query-generation provider itself failed; the plan was never
    # judged on its merits.
    QUERY_GENERATION_PROVIDER_ERROR = "query_generation_provider_error"
    # Every research search failed or timed out, so nothing was acquired.
    ACQUISITION_FAILED = "acquisition_failed"
    # The semantic judge could not be reached or malfunctioned.
    SEMANTIC_MATCHING_FAILED = "semantic_matching_failed"
    # The local budget expired before the run finished.
    TIMEOUT = "timeout"
    # The worker died -- most often a backend restart mid-run.
    INTERRUPTED = "interrupted"
    # Anything the orchestration could not absorb.
    UNEXPECTED_ERROR = "unexpected_error"

    @property
    def is_retryable(self) -> bool:
        """Whether repeating the operation could plausibly change this.

        Deliberately a property on the reason rather than a column: it is
        a fact ABOUT the reason, and storing it separately would let the
        two disagree.
        """
        return self in _RETRYABLE_OUTCOME_REASONS

    @property
    def is_success(self) -> bool:
        """Whether this reason accompanies a SUCCEEDED run."""
        return self in _SUCCESS_OUTCOME_REASONS


_SUCCESS_OUTCOME_REASONS = frozenset(
    {
        ResearchOutcomeReason.NO_RELEVANT_RESEARCH,
        ResearchOutcomeReason.ACQUISITION_PARTIAL,
    }
)

# A successful run is never "retryable": there is nothing to fix. A
# deterministic query rejection is not retryable either -- the same input
# deterministically produces the same plan.
_RETRYABLE_OUTCOME_REASONS = frozenset(
    {
        ResearchOutcomeReason.QUERY_GENERATION_PROVIDER_ERROR,
        ResearchOutcomeReason.ACQUISITION_FAILED,
        ResearchOutcomeReason.SEMANTIC_MATCHING_FAILED,
        ResearchOutcomeReason.TIMEOUT,
        ResearchOutcomeReason.INTERRUPTED,
        ResearchOutcomeReason.UNEXPECTED_ERROR,
    }
)


class ResearchQueryStatus(str, enum.Enum):
    """Lifecycle of ONE research query inside an enrichment, persisted.

    Finer-grained than ResearchEnrichmentStatus on purpose. The enrichment
    as a whole cannot honestly say "2 of 3 searches are done" unless each
    search reports itself, and the frontend is forbidden from inventing
    that progress with a timer -- so the backend has to be able to say it.

    TIMED_OUT is deliberately distinct from FAILED. A provider job that
    outran GapRadar's local patience is still running on Bright Data's
    side and returned nothing to us; a FAILED one was refused or errored.
    Collapsing them would hide the single most common demo failure.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ReliabilityState(str, enum.Enum):
    """A collector's CURRENT reliability, computed -- never persisted.

    HEALTHY means no active incident. Every other member mirrors the
    status of the collector's active incident. RECOVERED is absent on
    purpose: a recovered incident is closed, so it no longer describes
    the collector's present state.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    HEALING = "healing"
    VALIDATING = "validating"
    VERIFYING = "verifying"
    MANUAL_REVIEW = "manual_review"
