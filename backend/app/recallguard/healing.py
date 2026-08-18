"""One autonomous repair attempt, end to end.

    DEGRADED
      -> start_healing
      -> Bright Data self-heal trigger
      -> poll to the approval gate
      -> register_repair_candidate
      -> candidate preflight
           fail -> reject   -> DEGRADED
           pass -> approve  -> self-heal completes
      -> fresh production collection (the existing orchestrator)
      -> verify_recovery
           fail -> DEGRADED (next attempt, up to three)
           pass -> RECOVERED

What this module refuses to do is as important as what it does:

- Candidate preflight is NOT recovery. The provider's preview_result is
  evidence for whether a repair is worth approving, and nothing more.
- Approval is NOT recovery. It means the repair was authorized and
  deployed, not that it works.
- Only a fresh production CollectorRun, started after the repair, passing
  every RecallGuard check, can move an incident to RECOVERED -- and that
  judgement is made by app.recallguard.service.verify_recovery, not here.
- After a failed post-approval verification the approved candidate is
  never "un-approved". It is already deployed; the answer is another
  attempt, within the three-attempt budget.

The HTTP calls here are the same operations the Bright Data CLI performs,
so a demo can narrate them with:

    brightdata scraper heal <collector_id> "<prompt>"     (trigger)
    brightdata scraper approve <collector_id>             (approve)
    brightdata scraper approve <collector_id> --reject    (reject)

The CLI is a demo aid only. Application code never shells out, and
--auto-approve is deliberately not part of this flow: the candidate gate
is where RecallGuard earns its keep.
"""

import enum
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.collection.errors import CollectionError
from app.collection.schemas import DEFAULT_POLLING_POLICY, PollingPolicy
from app.collection.service import run_fix_my_itch_collection
from app.db.models import Collector, CollectorRun, ReliabilityIncident
from app.integrations.brightdata.client import BrightDataClient
from app.integrations.brightdata.errors import BrightDataError
from app.integrations.brightdata.fix_my_itch import validate_dataset
from app.integrations.brightdata.schemas import (
    HealingCandidate,
    HealingRequest,
    HealingStatus,
)
from app.recallguard.errors import RepairAttemptLimitExceededError
from app.recallguard.prompts import build_heal_prompt
from app.recallguard.schemas import (
    DEFAULT_POLICY,
    BaselineProfile,
    ReliabilityEvaluation,
    ReliabilityPolicy,
)
from app.recallguard.service import (
    escalate,
    record_healing_failure,
    register_repair_candidate,
    start_healing,
    verify_recovery,
)

logger = logging.getLogger(__name__)

# How many preview records are validated and how many failures are kept
# as evidence. The provider's preview is a sample, and an incident is a
# summary of it, not a second copy.
MAX_PREVIEW_VIOLATIONS_RECORDED = 5


class HealingOutcome(str, enum.Enum):
    """How one repair attempt ended. Computed, never persisted."""

    RECOVERED = "recovered"
    VERIFICATION_FAILED = "verification_failed"
    CANDIDATE_REJECTED = "candidate_rejected"
    PROVIDER_FAILED = "provider_failed"
    ESCALATED = "escalated"


class SelfHealingPolicy(BaseModel):
    """Local waiting policy for the provider's self-healing flow.

    Both values are GapRadar's own patience. Neither is ever sent to
    Bright Data -- a provider-side deadline once terminated a real
    production run mid-collection.
    """

    model_config = ConfigDict(frozen=True)

    interval_seconds: float = Field(default=10.0, gt=0)
    timeout_seconds: float = Field(default=900.0, gt=0)
    # Without a preview to validate, an autonomous approval would be a
    # guess. RecallGuard stops and asks a human instead.
    require_preview_for_approval: bool = True


DEFAULT_SELF_HEALING_POLICY = SelfHealingPolicy()


class HealingAttemptResult(BaseModel):
    """What one repair attempt achieved.

    `recovered` is true only when verify_recovery said so. Nothing in
    this module can set it any other way.
    """

    model_config = ConfigDict(frozen=True)

    incident_id: uuid.UUID
    attempt: int
    outcome: HealingOutcome
    candidate_approved: bool = False
    provider_status: HealingStatus | None = None
    verification_run_id: uuid.UUID | None = None
    recovered: bool = False
    evaluation: ReliabilityEvaluation | None = None
    detail: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def execute_healing_attempt(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    baseline: BaselineProfile | None = None,
    reliability_policy: ReliabilityPolicy = DEFAULT_POLICY,
    healing_policy: SelfHealingPolicy = DEFAULT_SELF_HEALING_POLICY,
    collection_polling: PollingPolicy = DEFAULT_POLLING_POLICY,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> HealingAttemptResult:
    """Run one autonomous repair attempt for a degraded incident.

    The incident's own state is the guard: start_healing refuses anything
    that is not DEGRADED with a repairable diagnosis, and it runs before
    the first provider call, so a recovered, in-flight, or non-repairable
    incident never reaches Bright Data. Calling this twice in a row
    therefore cannot start two attempts or approve the same candidate
    twice.
    """
    try:
        start_healing(session, incident, now=now)
    except RepairAttemptLimitExceededError as exc:
        # The incident is already MANUAL_REVIEW / ESCALATE. No provider
        # call is made: a fourth autonomous repair is exactly what the
        # limit exists to prevent.
        logger.warning(
            "healing_attempt_refused",
            extra={"incident_id": str(incident.id), "reason": "attempt_limit"},
        )
        return HealingAttemptResult(
            incident_id=incident.id,
            attempt=incident.repair_attempts,
            outcome=HealingOutcome.ESCALATED,
            detail=str(exc),
        )

    attempt = incident.repair_attempts
    prompt = build_heal_prompt(incident)

    candidate = _trigger_and_wait_for_gate(
        session,
        client,
        incident=incident,
        collector=collector,
        prompt=prompt,
        policy=healing_policy,
        now=now,
        sleep=sleep,
    )
    if isinstance(candidate, HealingAttemptResult):
        return candidate

    register_repair_candidate(
        session,
        incident,
        candidate=_candidate_evidence(candidate, attempt=attempt),
        now=now,
    )

    preflight = _preflight_candidate(
        candidate, source_id=collector.source_id, policy=healing_policy
    )
    if preflight.decision is _PreflightDecision.ESCALATE:
        # No preview means no basis to approve. The provider's approval
        # gate is deliberately left open so a human can inspect the
        # candidate and decide.
        escalate(
            session,
            incident,
            reason=preflight.reason,
            evidence={"attempt": attempt, "preflight": preflight.evidence},
            now=now,
        )
        return HealingAttemptResult(
            incident_id=incident.id,
            attempt=attempt,
            outcome=HealingOutcome.ESCALATED,
            provider_status=candidate.status,
            detail=preflight.reason,
        )

    if preflight.decision is _PreflightDecision.REJECT:
        return _reject_candidate(
            session,
            client,
            incident=incident,
            collector=collector,
            attempt=attempt,
            preflight=preflight,
            now=now,
        )

    approved = _approve_candidate(
        session,
        client,
        incident=incident,
        collector=collector,
        attempt=attempt,
        preflight=preflight,
        policy=healing_policy,
        now=now,
        sleep=sleep,
    )
    if isinstance(approved, HealingAttemptResult):
        return approved

    return _verify_with_fresh_collection(
        session,
        client,
        incident=incident,
        collector=collector,
        attempt=attempt,
        baseline=baseline,
        reliability_policy=reliability_policy,
        collection_polling=collection_polling,
        now=now,
        sleep=sleep,
    )


# -- provider stages --------------------------------------------------------


def _trigger_and_wait_for_gate(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    prompt: str,
    policy: SelfHealingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> HealingCandidate | HealingAttemptResult:
    """Ask Bright Data to repair the collector and wait for the gate."""
    try:
        client.request_healing(
            HealingRequest(collector_id=collector.external_collector_id, prompt=prompt)
        )
    except BrightDataError as exc:
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason="self_heal_trigger_failed",
            detail=str(exc),
            now=now,
        )

    logger.info(
        "self_healing_requested",
        extra={
            "incident_id": str(incident.id),
            "external_collector_id": collector.external_collector_id,
            "attempt": incident.repair_attempts,
            "prompt_chars": len(prompt),
        },
    )

    try:
        candidate = _poll_healing(
            client,
            collector.external_collector_id,
            policy=policy,
            now=now,
            sleep=sleep,
            until={HealingStatus.AWAITING_APPROVAL, HealingStatus.DONE},
        )
    except BrightDataError as exc:
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason="self_heal_progress_failed",
            detail=str(exc),
            now=now,
        )
    except TimeoutError as exc:
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason="self_heal_polling_timeout",
            detail=str(exc),
            now=now,
        )

    if candidate.status is HealingStatus.FAILED:
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason="self_heal_failed",
            detail="Bright Data reported the self-healing job as failed",
            provider_status=candidate.status,
            now=now,
        )

    if candidate.status is HealingStatus.DONE:
        # The flow finished without ever offering an approval gate, so
        # nothing was submitted for RecallGuard's decision. Rather than
        # accept a repair we never authorized, the incident goes back to
        # DEGRADED with the provider's account of what happened.
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason="self_heal_completed_without_approval_gate",
            detail=(
                "the self-healing job reached a terminal state without "
                "pausing for approval; no candidate was reviewed"
            ),
            provider_status=candidate.status,
            now=now,
        )

    return candidate


def _poll_healing(
    client: BrightDataClient,
    external_collector_id: str,
    *,
    policy: SelfHealingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    until: set[HealingStatus],
) -> HealingCandidate:
    """Poll self-healing progress until a status of interest or timeout.

    The single self-healing wait loop: interval and budget come from
    SelfHealingPolicy, waiting happens through the injected sleeper, and
    the budget is enforced locally.

    HealingStatus.UNKNOWN is treated as "still working", matching the
    reference CLI's own polling behavior -- Bright Data documents no
    explicit in-progress wire value, so an unrecognized status is not
    read as a verdict either way.
    """
    deadline = now() + timedelta(seconds=policy.timeout_seconds)
    polls = 0
    while True:
        polls += 1
        candidate = client.get_healing_status(external_collector_id)
        if candidate.status in until or candidate.status is HealingStatus.FAILED:
            return candidate
        if now() >= deadline:
            raise TimeoutError(
                f"local self-healing budget of {policy.timeout_seconds}s elapsed "
                f"after {polls} polls"
            )
        sleep(policy.interval_seconds)


def _approve_candidate(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    attempt: int,
    preflight: "_PreflightResult",
    policy: SelfHealingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> HealingCandidate | HealingAttemptResult:
    """Authorize the repair, then wait for the provider to commit it.

    Approval is where the repair gets deployed -- and where recovery
    explicitly does NOT happen. The incident stays VALIDATING.
    """
    try:
        client.approve_healing(collector.external_collector_id, auto_save=True)
        candidate = _poll_healing(
            client,
            collector.external_collector_id,
            policy=policy,
            now=now,
            sleep=sleep,
            until={HealingStatus.DONE},
        )
    except BrightDataError as exc:
        return _abandon(
            session,
            incident,
            attempt=attempt,
            reason="self_heal_approval_failed",
            detail=str(exc),
            now=now,
        )
    except TimeoutError as exc:
        return _abandon(
            session,
            incident,
            attempt=attempt,
            reason="self_heal_completion_timeout",
            detail=str(exc),
            now=now,
        )

    if candidate.status is not HealingStatus.DONE:
        return _abandon(
            session,
            incident,
            attempt=attempt,
            reason="self_heal_failed_after_approval",
            detail="Bright Data did not complete the approved repair",
            provider_status=candidate.status,
            now=now,
        )

    incident.evidence = _append_event(
        incident.evidence,
        {
            "event": "candidate_approved",
            "at": now().isoformat(),
            "attempt": attempt,
            "provider_status": candidate.status.value,
            "preflight": preflight.evidence,
            # Stated plainly so nobody reading the incident mistakes it
            # for a recovery record.
            "note": "repair deployed; recovery still requires a fresh run",
        },
    )
    session.commit()
    session.refresh(incident)
    logger.info(
        "self_healing_candidate_approved",
        extra={"incident_id": str(incident.id), "attempt": attempt},
    )
    return candidate


def _reject_candidate(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    attempt: int,
    preflight: "_PreflightResult",
    now: Callable[[], datetime],
) -> HealingAttemptResult:
    """Refuse a repair whose own preview already violates the contract."""
    detail = preflight.reason
    try:
        client.reject_healing(collector.external_collector_id)
    except BrightDataError as exc:
        detail = f"{preflight.reason}; provider reject also failed: {exc}"

    record_healing_failure(
        session,
        incident,
        reason="candidate_rejected",
        evidence={"attempt": attempt, "preflight": preflight.evidence},
        now=now,
    )
    logger.warning(
        "self_healing_candidate_rejected",
        extra={"incident_id": str(incident.id), "attempt": attempt},
    )
    return HealingAttemptResult(
        incident_id=incident.id,
        attempt=attempt,
        outcome=HealingOutcome.CANDIDATE_REJECTED,
        provider_status=HealingStatus.AWAITING_APPROVAL,
        detail=detail,
    )


def _verify_with_fresh_collection(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    attempt: int,
    baseline: BaselineProfile | None,
    reliability_policy: ReliabilityPolicy,
    collection_polling: PollingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> HealingAttemptResult:
    """The only thing that can establish recovery.

    A real production collection through the existing orchestrator --
    same collector id, same trigger contract, same atomic ingestion --
    producing a genuinely new CollectorRun, judged by the existing
    verify_recovery. No second collection path and no second verification
    implementation exists.
    """
    try:
        collection = run_fix_my_itch_collection(
            session,
            client,
            collector=collector,
            polling=collection_polling,
            now=now,
            sleep=sleep,
        )
    except CollectionError as exc:
        record_healing_failure(
            session,
            incident,
            reason="verification_collection_failed",
            evidence={
                "attempt": attempt,
                "stage": exc.stage,
                "error": type(exc).__name__,
                # The approved repair is deliberately left in place: it
                # is already deployed, and un-approving it is not an
                # operation Bright Data offers.
                "note": "approved repair remains deployed",
            },
            now=now,
        )
        return HealingAttemptResult(
            incident_id=incident.id,
            attempt=attempt,
            outcome=HealingOutcome.VERIFICATION_FAILED,
            candidate_approved=True,
            provider_status=HealingStatus.DONE,
            detail=str(exc),
        )

    verification_run = session.get(CollectorRun, collection.collector_run_id)
    if verification_run is None:  # pragma: no cover - defensive
        raise RuntimeError(
            f"collection run {collection.collector_run_id} vanished after ingestion"
        )

    evaluation = verify_recovery(
        session,
        incident,
        verification_run=verification_run,
        baseline=baseline,
        policy=reliability_policy,
        now=now,
    )

    logger.info(
        "healing_attempt_verified",
        extra={
            "incident_id": str(incident.id),
            "attempt": attempt,
            "verification_run_id": str(verification_run.id),
            "recovered": evaluation.passed,
        },
    )
    return HealingAttemptResult(
        incident_id=incident.id,
        attempt=attempt,
        outcome=(
            HealingOutcome.RECOVERED
            if evaluation.passed
            else HealingOutcome.VERIFICATION_FAILED
        ),
        candidate_approved=True,
        provider_status=HealingStatus.DONE,
        verification_run_id=verification_run.id,
        recovered=evaluation.passed,
        evaluation=evaluation,
    )


# -- candidate preflight ----------------------------------------------------


class _PreflightDecision(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"


class _PreflightResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: _PreflightDecision
    reason: str
    evidence: dict[str, Any]


def _preflight_candidate(
    candidate: HealingCandidate,
    *,
    source_id: uuid.UUID,
    policy: SelfHealingPolicy,
) -> _PreflightResult:
    """Judge a proposed repair by its own preview -- fail-closed.

    The preview is run through the same strict source-native validation
    production data faces. One bad preview record rejects the candidate:
    a repair that still returns a tam_score of 60, an unknown field, or a
    missing problem is not worth deploying.

    This is evidence for an approval decision and nothing more. Passing
    preflight does not recover an incident, and the preview is never
    ingested.
    """
    preview = candidate.preview_result
    if not isinstance(preview, list) or not preview:
        if policy.require_preview_for_approval:
            return _PreflightResult(
                decision=_PreflightDecision.ESCALATE,
                reason=(
                    "no preview_result to validate; refusing to approve a "
                    "repair candidate unseen"
                ),
                evidence={
                    "preview_records": 0,
                    "has_diff": candidate.diff is not None,
                },
            )
        return _PreflightResult(
            decision=_PreflightDecision.APPROVE,
            reason="no preview available; approval not gated by preview",
            evidence={"preview_records": 0},
        )

    report = validate_dataset(preview, source_id=source_id)
    evidence: dict[str, Any] = {
        "preview_records": len(preview),
        "valid_records": len(report.valid),
        "invalid_records": len(report.invalid),
        "violations": [
            invalid.model_dump(mode="json")
            for invalid in report.invalid[:MAX_PREVIEW_VIOLATIONS_RECORDED]
        ],
    }
    if report.invalid:
        first = report.invalid[0]
        return _PreflightResult(
            decision=_PreflightDecision.REJECT,
            reason=(
                f"{len(report.invalid)} of {len(preview)} preview records still "
                f"violate the source contract (first: index {first.index}, "
                f"{first.reason.value})"
            ),
            evidence=evidence,
        )
    return _PreflightResult(
        decision=_PreflightDecision.APPROVE,
        reason=f"all {len(preview)} preview records satisfy the source contract",
        evidence=evidence,
    )


# -- evidence ---------------------------------------------------------------


def _candidate_evidence(candidate: HealingCandidate, *, attempt: int) -> dict[str, Any]:
    """Bounded provider metadata. Never the whole preview payload."""
    preview = candidate.preview_result
    metadata = candidate.provider_metadata or {}
    return {
        "attempt": attempt,
        "provider_status": candidate.status.value,
        "step": metadata.get("step"),
        "completed_steps": metadata.get("completed_steps"),
        "preview_records": len(preview) if isinstance(preview, list) else 0,
        "has_diff": candidate.diff is not None,
    }


def _abandon(
    session: Session,
    incident: ReliabilityIncident,
    *,
    attempt: int,
    reason: str,
    detail: str,
    now: Callable[[], datetime],
    provider_status: HealingStatus | None = None,
) -> HealingAttemptResult:
    """End the attempt without a verdict on the repair itself."""
    record_healing_failure(
        session,
        incident,
        reason=reason,
        evidence={"attempt": attempt, "detail": detail},
        now=now,
    )
    return HealingAttemptResult(
        incident_id=incident.id,
        attempt=attempt,
        outcome=HealingOutcome.PROVIDER_FAILED,
        provider_status=provider_status,
        detail=detail,
    )


def _append_event(
    evidence: dict[str, Any] | None, event: dict[str, Any]
) -> dict[str, Any]:
    current = dict(evidence or {})
    events = list(current.get("events") or [])
    events.append(event)
    current["events"] = events
    return current
