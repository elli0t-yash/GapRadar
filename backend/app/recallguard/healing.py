"""One autonomous repair attempt, end to end -- and how to rejoin one.

    DEGRADED
      -> ask the provider whether a repair is ALREADY in flight
           yes -> resume_healing        (no trigger, no attempt consumed)
           no  -> start_healing         (attempt consumed, budget applies)
                  -> Bright Data self-heal trigger
      -> poll to the approval gate
      -> register_repair_candidate
      -> candidate preflight
           fail -> reject   -> DEGRADED (attempt spent; MANUAL_REVIEW
                               only once the budget is gone)
           pass -> approve  -> self-heal completes
      -> fresh production collection (the existing orchestrator)
      -> verify_recovery
           fail -> DEGRADED (next attempt, up to three)
           pass -> RECOVERED

Resuming exists because a local timeout is not a provider failure. Bright
Data keeps repairing after GapRadar stops waiting, after the process
exits, and after a deployment restarts, so the provider -- not GapRadar's
own record -- is the authority on whether a repair is still running.
Asking it first is what stops a second refactor_template being posted on
top of a live repair, and why attempt 1 stays attempt 1 across however
many restarts it takes.

What this module refuses to do is as important as what it does:

- Candidate preflight is NOT recovery. The provider's preview_result is
  evidence for whether a repair is worth approving, and nothing more.
- A candidate that fails preflight -- including one that arrives with no
  preview at all -- is REJECTED, not escalated. It is one bad repair
  proposal, and RecallGuard has three attempts to find a good one. Only
  an exhausted budget, or a state no autonomous action can resolve,
  hands an incident to a human.
- Approval is NOT recovery. It means the repair was authorized and
  deployed, not that it works.
- Only a fresh production CollectorRun, started after the repair, passing
  every RecallGuard check, can move an incident to RECOVERED -- and that
  judgement is made by app.recallguard.service.verify_recovery, not here.
  A resumed repair earns recovery the same way; nothing is waived because
  the attempt began in an earlier process.
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
from app.domain.enums import IncidentStatus, RecommendedAction
from app.integrations.brightdata.client import BrightDataClient
from app.integrations.brightdata.errors import (
    BrightDataError,
    BrightDataNotFoundError,
)
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
    MAX_AUTONOMOUS_REPAIR_ATTEMPTS,
    RESUMABLE_INCIDENT_STATUSES,
    escalate,
    record_healing_failure,
    register_repair_candidate,
    resume_healing,
    start_healing,
    verify_recovery,
)

logger = logging.getLogger(__name__)

# How many preview records are validated and how many failures are kept
# as evidence. The provider's preview is a sample, and an incident is a
# summary of it, not a second copy.
MAX_PREVIEW_VIOLATIONS_RECORDED = 5

# Provider statuses that mean a repair is genuinely in flight right now.
# Only these justify resuming, and only these forbid triggering a new
# repair. DONE and FAILED are terminal states of a PAST repair -- the
# progress endpoint keeps reporting them until the next trigger, so
# treating either as "in flight" would deadlock the collector forever.
# UNKNOWN is deliberately absent: an unreadable state is not evidence
# that a repair is running, and it is not evidence that none is.
_ACTIVE_PROVIDER_STATUSES = frozenset(
    {HealingStatus.RUNNING, HealingStatus.AWAITING_APPROVAL}
)


class HealingOutcome(str, enum.Enum):
    """How one repair attempt ended. Computed, never persisted."""

    RECOVERED = "recovered"
    VERIFICATION_FAILED = "verification_failed"
    CANDIDATE_REJECTED = "candidate_rejected"
    # Bright Data itself failed: it reported a terminal failure status, or
    # a call to it errored. Never used for GapRadar running out of
    # patience.
    PROVIDER_FAILED = "provider_failed"
    ESCALATED = "escalated"
    # GapRadar stopped waiting. The provider repair is not known to have
    # failed and is most likely still working, so the incident stays
    # resumable and the attempt is not spent again on a new trigger.
    LOCAL_TIMEOUT = "local_timeout"
    # RecallGuard declined to act at all: nothing was triggered, nothing
    # was resumed, and the incident was left exactly as it was. Used when
    # the provider's state cannot be read or does not belong to us.
    REFUSED = "refused"


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
    # guess. RecallGuard rejects the candidate instead, spending the
    # attempt rather than escalating a repair that has two tries left.
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


class _LocalPollTimeout(TimeoutError):
    """GapRadar's own patience ran out while polling the provider.

    Subclasses TimeoutError so existing handling still catches it, and
    carries the last status actually observed -- the difference between
    "we stopped waiting on a repair that was still running" and "the
    provider failed" is exactly what this exception exists to preserve.
    """

    def __init__(self, message: str, *, last_status: HealingStatus | None) -> None:
        self.last_status = last_status
        super().__init__(message)


def resume_or_execute_healing_attempt(
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
    """Continue the repair Bright Data is already running, or start one.

    The entrypoint every scheduler, API call, and pipeline run should
    use. It asks the provider what is actually happening BEFORE deciding,
    because GapRadar's own record cannot answer that question: a local
    timeout, a process exit, or a deployment restart all leave an
    incident back at DEGRADED while the provider's repair carries on.
    Trusting that record alone is what would post refactor_template a
    second time on top of a live repair.

    The decision, in full:

    - RUNNING or AWAITING_APPROVAL -> resume. No trigger, no attempt
      consumed, and the candidate path continues exactly where it was.
    - UNKNOWN, or the status cannot be read at all -> refuse. Nothing is
      triggered and nothing is written: an unreadable provider is not a
      licence to start work that might duplicate a live repair.
    - DONE, FAILED, or no self-healing job at all -> nothing is in
      flight, so a genuinely new attempt may start, with the ordinary
      three-attempt budget applying.

    What resuming does NOT skip: candidate registration, the strict
    preview preflight, explicit approval only on a PASS, a fresh
    production collection, and verify_recovery. Bright Data reporting
    done still never recovers an incident.
    """
    if incident.recommended_action is not RecommendedAction.REQUEST_HEAL or (
        incident.status not in RESUMABLE_INCIDENT_STATUSES
    ):
        # Not something healing may touch (an outage, an escalated
        # incident, a closed one). Delegated unchanged to the existing
        # refusal so that rule has one definition -- and no provider call
        # is made on the way to it.
        return execute_healing_attempt(
            session,
            client,
            incident=incident,
            collector=collector,
            baseline=baseline,
            reliability_policy=reliability_policy,
            healing_policy=healing_policy,
            collection_polling=collection_polling,
            now=now,
            sleep=sleep,
        )

    observation = _observe_provider_repair(client, collector)
    if observation.unreadable is not None:
        return _refuse(
            incident,
            detail=(
                "could not read the provider's self-healing status, so a new "
                f"repair was not triggered: {observation.unreadable}"
            ),
        )

    candidate = observation.candidate
    status = candidate.status if candidate else None

    if candidate is not None and status in _ACTIVE_PROVIDER_STATUSES:
        if incident.repair_attempts < 1:
            # A repair is running that GapRadar never started. Adopting
            # it would claim someone else's work; triggering another
            # would duplicate it. Neither is acceptable, so nothing is
            # done and the next invocation looks again.
            return _refuse(
                incident,
                detail=(
                    "a self-healing job is already in flight for this collector "
                    "but no GapRadar repair attempt is open for it; refusing to "
                    "trigger a second repair"
                ),
                provider_status=status,
            )
        return _resume_existing_repair(
            session,
            client,
            incident=incident,
            collector=collector,
            candidate=candidate,
            baseline=baseline,
            reliability_policy=reliability_policy,
            healing_policy=healing_policy,
            collection_polling=collection_polling,
            now=now,
            sleep=sleep,
        )

    if status is HealingStatus.UNKNOWN:
        # Fail closed. An unrecognized status might be a live repair
        # under a name we do not know yet.
        return _refuse(
            incident,
            detail=(
                "the provider reported an unrecognized self-healing status; "
                "refusing to trigger a repair until it is readable"
            ),
            provider_status=status,
        )

    # Nothing is in flight: no job, or a terminal state left over from a
    # previous repair.
    if incident.status is not IncidentStatus.DEGRADED:
        # The incident thinks a repair is in progress and the provider
        # says otherwise -- a process died mid-attempt. Record that the
        # attempt ended and hand the incident back as DEGRADED; the next
        # invocation may start the next attempt within the budget.
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason=(
                "self_heal_completed_without_approval_gate"
                if status is HealingStatus.DONE
                else "provider_repair_no_longer_active"
            ),
            detail=(
                "the provider reports no repair in flight for this collector "
                f"(status: {status.value if status else 'none'}), so the "
                "in-flight attempt recorded here cannot be resumed"
            ),
            provider_status=status,
            now=now,
        )

    return execute_healing_attempt(
        session,
        client,
        incident=incident,
        collector=collector,
        baseline=baseline,
        reliability_policy=reliability_policy,
        healing_policy=healing_policy,
        collection_polling=collection_polling,
        now=now,
        sleep=sleep,
    )


class _ProviderObservation(BaseModel):
    """What the provider says about this collector's repair, if anything.

    `candidate` is None when no self-healing job exists. `unreadable`
    carries the error when the question could not be answered at all --
    a state that must never be mistaken for "no repair is running".
    """

    model_config = ConfigDict(frozen=True)

    candidate: HealingCandidate | None = None
    unreadable: str | None = None


def _observe_provider_repair(
    client: BrightDataClient, collector: Collector
) -> _ProviderObservation:
    """Ask Bright Data whether a repair is in flight. Reads only.

    Reuses the existing collector-scoped progress endpoint
    (GET /dca/collectors/{id}/refactor_template/progress); no new
    provider capability is invented, and no state is stored locally to
    track what the provider is doing -- the provider is the authority on
    its own work.
    """
    try:
        candidate = client.get_healing_status(collector.external_collector_id)
    except BrightDataNotFoundError:
        # No self-healing job has ever run for this collector. A 404
        # cannot mean "a repair is running", so a new one is safe.
        return _ProviderObservation()
    except BrightDataError as exc:
        return _ProviderObservation(unreadable=str(exc))
    return _ProviderObservation(candidate=candidate)


def _resume_existing_repair(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    candidate: HealingCandidate,
    baseline: BaselineProfile | None,
    reliability_policy: ReliabilityPolicy,
    healing_policy: SelfHealingPolicy,
    collection_polling: PollingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> HealingAttemptResult:
    """Re-enter the attempt already running at the provider.

    resume_healing consumes no attempt, so attempt 1 stays attempt 1
    however many local timeouts or restarts it takes. request_healing is
    not called and cannot be reached from here.
    """
    resume_healing(session, incident, now=now)
    attempt = incident.repair_attempts
    logger.info(
        "self_healing_attempt_resumed",
        extra={
            "incident_id": str(incident.id),
            "external_collector_id": collector.external_collector_id,
            "attempt": attempt,
            "provider_status": candidate.status.value,
        },
    )

    if candidate.status is HealingStatus.RUNNING:
        gate = _wait_for_gate(
            session,
            client,
            incident=incident,
            collector=collector,
            policy=healing_policy,
            now=now,
            sleep=sleep,
        )
        if isinstance(gate, HealingAttemptResult):
            return gate
        candidate = gate

    return _decide_and_verify(
        session,
        client,
        incident=incident,
        collector=collector,
        attempt=attempt,
        candidate=candidate,
        baseline=baseline,
        reliability_policy=reliability_policy,
        healing_policy=healing_policy,
        collection_polling=collection_polling,
        now=now,
        sleep=sleep,
    )


def _refuse(
    incident: ReliabilityIncident,
    *,
    detail: str,
    provider_status: HealingStatus | None = None,
) -> HealingAttemptResult:
    """Decline to act, leaving the incident untouched.

    Writes nothing: no attempt consumed, no status change, no evidence
    event. The incident stays exactly as it was so the next invocation
    can make the same decision with better information.
    """
    logger.warning(
        "healing_attempt_refused",
        extra={
            "incident_id": str(incident.id),
            "attempt": incident.repair_attempts,
            "provider_status": provider_status.value if provider_status else None,
        },
    )
    return HealingAttemptResult(
        incident_id=incident.id,
        attempt=incident.repair_attempts,
        outcome=HealingOutcome.REFUSED,
        provider_status=provider_status,
        detail=detail,
    )


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
    if incident.status is IncidentStatus.MANUAL_REVIEW:
        # A human owns this incident. Autonomous repair does not take it
        # back, and does not raise about it either: the answer to "may I
        # heal this?" is a plain no, the same answer the attempt-limit
        # branch below gives.
        logger.warning(
            "healing_attempt_refused",
            extra={"incident_id": str(incident.id), "reason": "manual_review"},
        )
        return HealingAttemptResult(
            incident_id=incident.id,
            attempt=incident.repair_attempts,
            outcome=HealingOutcome.ESCALATED,
            detail=(
                f"incident {incident.id} is in manual review; autonomous repair "
                "does not take it back"
            ),
        )

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

    return _decide_and_verify(
        session,
        client,
        incident=incident,
        collector=collector,
        attempt=attempt,
        candidate=candidate,
        baseline=baseline,
        reliability_policy=reliability_policy,
        healing_policy=healing_policy,
        collection_polling=collection_polling,
        now=now,
        sleep=sleep,
    )


def _decide_and_verify(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    attempt: int,
    candidate: HealingCandidate,
    baseline: BaselineProfile | None,
    reliability_policy: ReliabilityPolicy,
    healing_policy: SelfHealingPolicy,
    collection_polling: PollingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> HealingAttemptResult:
    """Everything downstream of the provider's approval gate.

    Shared verbatim by a new attempt and by a resumed one, so a resumed
    repair faces exactly the same gates: the candidate is registered as
    existing (not as working), its preview is validated against the same
    strict source contract, approval happens only on a PASS, and recovery
    still requires a fresh production collection judged by
    verify_recovery. Resuming skips the trigger, never a guarantee.
    """
    register_repair_candidate(
        session,
        incident,
        candidate=_candidate_evidence(candidate, attempt=attempt),
        now=now,
    )

    preflight = _preflight_candidate(
        candidate, source_id=collector.source_id, policy=healing_policy
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
    """Ask Bright Data to repair the collector and wait for the gate.

    The ONLY place request_healing is called. A resumed repair reaches
    the gate through _wait_for_gate instead, so resuming can never post
    refactor_template a second time.
    """
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
    return _wait_for_gate(
        session,
        client,
        incident=incident,
        collector=collector,
        policy=policy,
        now=now,
        sleep=sleep,
    )


def _wait_for_gate(
    session: Session,
    client: BrightDataClient,
    *,
    incident: ReliabilityIncident,
    collector: Collector,
    policy: SelfHealingPolicy,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> HealingCandidate | HealingAttemptResult:
    """Wait for an already-running repair to reach the approval gate.

    Makes no trigger call of its own, so it is safe to enter for a repair
    GapRadar started in an earlier process.
    """
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
    except _LocalPollTimeout as exc:
        # GapRadar stopped waiting; Bright Data did not fail. The
        # incident goes back to DEGRADED precisely so the next
        # invocation can find this same repair and resume it, and the
        # attempt is not spent again.
        return _abandon(
            session,
            incident,
            attempt=incident.repair_attempts,
            reason="local_polling_timeout",
            detail=str(exc),
            provider_status=exc.last_status,
            outcome=HealingOutcome.LOCAL_TIMEOUT,
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

    HealingStatus.RUNNING and HealingStatus.UNKNOWN are both treated as
    "keep waiting": the first because the provider says so, the second
    because an unrecognized status is not read as a verdict either way,
    matching the reference CLI's own polling behavior. They are kept
    apart everywhere a decision is made, because only RUNNING is
    evidence that a repair is genuinely in flight.
    """
    deadline = now() + timedelta(seconds=policy.timeout_seconds)
    polls = 0
    last_status: HealingStatus | None = None
    while True:
        polls += 1
        candidate = client.get_healing_status(external_collector_id)
        last_status = candidate.status
        if candidate.status in until or candidate.status is HealingStatus.FAILED:
            return candidate
        if now() >= deadline:
            raise _LocalPollTimeout(
                f"local self-healing budget of {policy.timeout_seconds}s elapsed "
                f"after {polls} polls",
                last_status=last_status,
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
    except _LocalPollTimeout as exc:
        return _abandon(
            session,
            incident,
            attempt=attempt,
            reason="self_heal_completion_timeout",
            detail=str(exc),
            provider_status=exc.last_status,
            outcome=HealingOutcome.LOCAL_TIMEOUT,
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
    """Refuse a repair that cannot show it works, and close the gate.

    Every rejection path leads here: a preview that still violates the
    source contract, and a candidate that arrived with no preview at all.
    Both mean the same thing -- this proposal is not approvable -- so
    both get the same treatment. The provider's gate is closed with an
    explicit reject rather than left hanging, because an abandoned gate
    keeps the repair reported as in flight and blocks the next attempt
    from ever starting.

    A rejection is an unsuccessful ATTEMPT, not a reason to summon a
    human. The attempt is spent, the incident returns to DEGRADED still
    classified as drift and still recommending REQUEST_HEAL, and the next
    invocation may try again. Only when that spends the last of the
    three-attempt budget does the incident go to a person -- which is the
    one thing the budget exists to decide.
    """
    detail = preflight.reason
    rejected = True
    try:
        client.reject_healing(collector.external_collector_id)
    except BrightDataError as exc:
        # The candidate is still refused locally; it simply could not be
        # closed at the provider. Recorded as such so an operator knows
        # the gate may still be open over there.
        rejected = False
        detail = f"{preflight.reason}; provider reject also failed: {exc}"

    record_healing_failure(
        session,
        incident,
        reason="candidate_rejected",
        evidence={
            "attempt": attempt,
            "preflight_reason": preflight.reason,
            "preflight": preflight.evidence,
            "provider_rejected": rejected,
        },
        now=now,
    )
    logger.warning(
        "self_healing_candidate_rejected",
        extra={
            "incident_id": str(incident.id),
            "attempt": attempt,
            "provider_rejected": rejected,
        },
    )

    if attempt >= MAX_AUTONOMOUS_REPAIR_ATTEMPTS:
        # The last attempt is spent. Escalating here rather than waiting
        # for the next invocation to discover it keeps the incident
        # honest: DEGRADED / REQUEST_HEAL would advertise a repair that
        # can no longer be attempted.
        escalate(
            session,
            incident,
            reason=(
                f"{preflight.reason}; the {MAX_AUTONOMOUS_REPAIR_ATTEMPTS}-attempt "
                "autonomous repair budget is now exhausted"
            ),
            evidence={"attempt": attempt, "preflight": preflight.evidence},
            now=now,
        )
        return HealingAttemptResult(
            incident_id=incident.id,
            attempt=attempt,
            outcome=HealingOutcome.ESCALATED,
            provider_status=HealingStatus.AWAITING_APPROVAL,
            detail=detail,
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
    """Approve the candidate, or reject it. There is no third answer.

    Preflight never escalates. Anything wrong with a candidate -- a
    preview that violates the contract, or no preview at all -- makes it
    one unusable repair proposal, and unusable proposals are rejected.
    Whether that warrants a human is a question about the attempt budget,
    answered in _reject_candidate, not about this one candidate.
    """

    APPROVE = "approve"
    REJECT = "reject"


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
            # An unseen candidate is a failed repair proposal, not a
            # question for a human. It is rejected like any other
            # candidate that cannot show it works, which spends the
            # attempt and leaves the budget to decide when a person is
            # actually needed.
            return _PreflightResult(
                decision=_PreflightDecision.REJECT,
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
    outcome: HealingOutcome = HealingOutcome.PROVIDER_FAILED,
) -> HealingAttemptResult:
    """End this local attempt without a verdict on the repair itself.

    The incident returns to DEGRADED, which is what makes it eligible to
    be picked up again -- as a resume when the provider repair is still
    alive, or as the next attempt when it is not. `outcome` distinguishes
    the two reasons for stopping: PROVIDER_FAILED means Bright Data
    actually failed, LOCAL_TIMEOUT means only that GapRadar stopped
    waiting.
    """
    record_healing_failure(
        session,
        incident,
        reason=reason,
        evidence={
            "attempt": attempt,
            "detail": detail,
            "provider_status": provider_status.value if provider_status else None,
            # Recorded so an operator reading the incident can tell a
            # dead repair from one GapRadar merely stopped watching.
            "provider_failed": outcome is HealingOutcome.PROVIDER_FAILED,
        },
        now=now,
    )
    return HealingAttemptResult(
        incident_id=incident.id,
        attempt=attempt,
        outcome=outcome,
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
