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
