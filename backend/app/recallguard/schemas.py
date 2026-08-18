import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    FailureClassification,
    RecommendedAction,
    ReliabilityState,
)


class CheckResult(BaseModel):
    """One deterministic reliability check.

    Always records what was expected and what was observed, so an
    incident explains itself without anyone re-deriving the reasoning.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    expected: str
    observed: str
    detail: str | None = None


class BaselineProfile(BaseModel):
    """Observations of what a healthy collection looked like.

    Explicitly NOT a contract. Record counts and industry vocabularies
    move on their own; the source contract (score ranges, required
    fields, exact source/source_url, no unknown fields) is enforced
    elsewhere and is the only thing treated as immutable.

    A baseline is supplied by the caller rather than hardcoded, so
    nothing in the application pretends the current size of the source is
    permanent.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    record_count: int = Field(ge=0)
    industry_count: int = Field(default=0, ge=0)


class ReliabilityPolicy(BaseModel):
    """Operational heuristics for completeness. Tunable, not contractual.

    - A collection returning zero records when the baseline was non-zero
      is always a completeness failure. This is the conservative rule the
      MVP relies on.
    - `max_relative_record_drop` additionally fails a run whose record
      count fell by more than this fraction of the baseline. It is a
      judgement call about what "too few" means, not a fact about the
      source, so it is named, configurable, and can be switched off with
      None. A drop exactly equal to the threshold passes.
    - Growth is never penalized, and neither are new, removed, or
      reordered industries.
    """

    model_config = ConfigDict(frozen=True)

    max_relative_record_drop: float | None = Field(default=0.5, ge=0.0, le=1.0)


DEFAULT_POLICY = ReliabilityPolicy()


class ReliabilityEvaluation(BaseModel):
    """The verdict on one collector run.

    `passed` is about the run. `state` is about the collector: a run can
    pass every check while the collector stays DEGRADED, because only an
    explicit verification can close an incident.
    """

    model_config = ConfigDict(frozen=True)

    collector_run_id: uuid.UUID | None
    passed: bool
    state: ReliabilityState
    classification: FailureClassification | None = None
    recommended_action: RecommendedAction | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    incident_id: uuid.UUID | None = None

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]


class RecoveryProof(BaseModel):
    """Evidence that an independent collection re-established the contract.

    Deliberately free of any model score or confidence value: this is a
    record of which checks ran against which runs, reproducible by anyone
    reading it.
    """

    model_config = ConfigDict(frozen=True)

    incident_id: uuid.UUID
    collector_id: uuid.UUID
    detection_run_id: uuid.UUID | None
    verification_run_id: uuid.UUID
    classification: FailureClassification
    repair_attempt: int
    checks: list[CheckResult]
    verified_at: datetime
    result: str = "pass"


def profile_from_records(
    records: Sequence[dict[str, Any]], *, label: str
) -> BaselineProfile:
    """Derive a baseline from an observed healthy dataset.

    Used so callers and tests can capture a baseline from a real payload
    (e.g. the committed healthy fixture) instead of hardcoding counts
    that would quietly become a pseudo-contract.
    """
    industries = {
        record["industry"]
        for record in records
        if isinstance(record.get("industry"), str)
    }
    return BaselineProfile(
        label=label, record_count=len(records), industry_count=len(industries)
    )
