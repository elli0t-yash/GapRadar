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
