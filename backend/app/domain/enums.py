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
