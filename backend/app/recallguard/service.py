"""RecallGuard: observe, detect, diagnose, and prove repair.

    AI proposes. RecallGuard proves.

The rules this module exists to enforce:

- A CollectorRun reaching SUCCEEDED is an execution fact, never a trust
  verdict. This module can call a technically successful run degraded,
  and it never changes the run's own status to say so.
- A repair can never declare itself successful, and an approved repair is
  not a recovered one. Only a fresh, independent collection started after
  the repair, passing every check, moves an incident to RECOVERED.
- Autonomous repair is capped. After three attempts the incident goes to
  MANUAL_REVIEW and a human decides.
- Source data is never modified. Offending values are preserved verbatim
  as evidence.

Healing itself lives elsewhere: nothing here asks Bright Data to repair,
approve, or reject anything. This module only records that a repair
candidate exists and then judges it on results.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CollectorRun, ReliabilityIncident
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
    RunStatus,
)
from app.recallguard.detection import (
    diagnose,
    evaluate_run_checks,
    observed_record_count,
    orchestration,
    sample_violations,
)
from app.recallguard.errors import (
    IncidentTransitionError,
    NonTerminalRunError,
    RepairAttemptLimitExceededError,
    VerificationRunRejectedError,
)
from app.recallguard.schemas import (
    DEFAULT_POLICY,
    BaselineProfile,
    CheckResult,
    RecoveryProof,
    ReliabilityEvaluation,
    ReliabilityPolicy,
)

logger = logging.getLogger(__name__)

# Autonomous repair cycles allowed per incident. A fourth is refused: an
# automation that has failed three times needs a human, not a retry.
MAX_AUTONOMOUS_REPAIR_ATTEMPTS = 3

# An incident stops being active once it is proven repaired. Every other
# status -- including MANUAL_REVIEW -- still describes a live problem.
_ACTIVE_STATUSES = frozenset(IncidentStatus) - {IncidentStatus.RECOVERED}

# Statuses whose meaning is owned by the healing lifecycle. Re-evaluating
# a run while one of these is in force adds evidence but never rewinds
# the incident to DEGRADED behind the lifecycle's back.
_LIFECYCLE_STATUSES = frozenset(
    {IncidentStatus.HEALING, IncidentStatus.VALIDATING, IncidentStatus.VERIFYING}
)

# Only these conditions justify asking for a scraper repair. A provider
# outage or an internal ingestion failure is not repairable by healing
# the scraper, so autonomous healing is refused for them.
_REPAIRABLE_ACTIONS = frozenset({RecommendedAction.REQUEST_HEAL})

# Bounded so an incident stays readable no matter how often a broken
# collector runs.
MAX_RECORDED_OCCURRENCES = 20


def _utcnow() -> datetime:
    return datetime.now(UTC)


# -- observation ------------------------------------------------------------


def evaluate_collector_run(
    session: Session,
    *,
    run: CollectorRun,
    baseline: BaselineProfile | None = None,
    policy: ReliabilityPolicy = DEFAULT_POLICY,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityEvaluation:
    """Judge one terminal collector run and record what it means.

    Reads only evidence the collection orchestrator already persisted.
    Signals, records, and the run's own execution status are never
    modified -- a successful run that fails reliability checks stays
    SUCCEEDED, because the execution really did succeed.

    A failing run opens or updates the collector's single active
    incident. A passing run does NOT close one: an incident is closed
    only by verify_recovery, on the strength of a fresh independent run.
    """
    if run.status not in (RunStatus.SUCCEEDED, RunStatus.FAILED):
        raise NonTerminalRunError(
            f"collector run {run.id} is {run.status.value}; only a terminal "
            "run can be evaluated"
        )

    checks = evaluate_run_checks(run, baseline=baseline, policy=policy)
    passed = all(check.passed for check in checks)
    active = active_incident(session, collector_id=run.collector_id)

    if passed:
        logger.info(
            "reliability_run_passed",
            extra={
                "collector_id": str(run.collector_id),
                "collector_run_id": str(run.id),
                "active_incident_id": str(active.id) if active else None,
            },
        )
        return ReliabilityEvaluation(
            collector_run_id=run.id,
            passed=True,
            # A clean run does not clear an incident on its own.
            state=_state_for(active),
            checks=checks,
            incident_id=active.id if active else None,
        )

    classification, action = diagnose(run, checks)
    incident = _open_or_update_incident(
        session,
        run=run,
        checks=checks,
        classification=classification,
        action=action,
        active=active,
        baseline=baseline,
        now=now,
    )

    logger.warning(
        "reliability_degradation_detected",
        extra={
            "collector_id": str(run.collector_id),
            "collector_run_id": str(run.id),
            "incident_id": str(incident.id),
            "classification": classification.value,
            "recommended_action": incident.recommended_action.value,
        },
    )
    return ReliabilityEvaluation(
        collector_run_id=run.id,
        passed=False,
        state=_state_for(incident),
        classification=incident.classification,
        recommended_action=incident.recommended_action,
        checks=checks,
        incident_id=incident.id,
    )


def active_incident(
    session: Session, *, collector_id: uuid.UUID
) -> ReliabilityIncident | None:
    """The collector's one unresolved incident, if any.

    At most one active incident per collector: repeated failures of the
    same unresolved problem accumulate as evidence on that incident
    instead of spawning a new row each time a broken collector runs.
    """
    return session.execute(
        select(ReliabilityIncident)
        .where(
            ReliabilityIncident.collector_id == collector_id,
            ReliabilityIncident.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(ReliabilityIncident.detected_at.desc())
    ).scalar()


def collectors_with_active_incidents(session: Session) -> set[uuid.UUID]:
    """Every collector that currently has an unresolved incident.

    The set complement of "healthy". Exposed so downstream consumers can
    exclude untrusted collectors in one query instead of restating what
    counts as an active incident -- that definition stays here, where the
    trust rules live.
    """
    return set(
        session.execute(
            select(ReliabilityIncident.collector_id)
            .where(ReliabilityIncident.status.in_(_ACTIVE_STATUSES))
            .distinct()
        ).scalars()
    )


def collector_reliability_state(
    session: Session, *, collector_id: uuid.UUID
) -> ReliabilityState:
    """The collector's CURRENT reliability.

    HEALTHY means no active incident -- including when the collector has
    RECOVERED incidents in its history. A proven repair is a permanent
    record, not something erased to make the present look clean.
    """
    return _state_for(active_incident(session, collector_id=collector_id))


def _state_for(incident: ReliabilityIncident | None) -> ReliabilityState:
    if incident is None:
        return ReliabilityState.HEALTHY
    return ReliabilityState(incident.status.value)


def _open_or_update_incident(
    session: Session,
    *,
    run: CollectorRun,
    checks: list[CheckResult],
    classification: FailureClassification,
    action: RecommendedAction,
    active: ReliabilityIncident | None,
    baseline: BaselineProfile | None,
    now: Callable[[], datetime],
) -> ReliabilityIncident:
    occurrence = _occurrence(
        run=run,
        checks=checks,
        classification=classification,
        action=action,
        baseline=baseline,
        at=now(),
    )

    if active is None:
        incident = ReliabilityIncident(
            collector_id=run.collector_id,
            detection_run_id=run.id,
            status=IncidentStatus.DEGRADED,
            classification=classification,
            recommended_action=action,
            repair_attempts=0,
            detected_at=now(),
            evidence={"occurrences": [occurrence]},
        )
        session.add(incident)
        session.commit()
        session.refresh(incident)
        return incident

    active.evidence = _append_occurrence(active.evidence, occurrence)
    # A repeat failure while a repair is in flight is evidence, not a
    # reason to rewind the lifecycle. MANUAL_REVIEW likewise stays put:
    # only a human takes it out of that state.
    if active.status not in _LIFECYCLE_STATUSES | {IncidentStatus.MANUAL_REVIEW}:
        active.classification = classification
        active.recommended_action = action
    session.commit()
    session.refresh(active)
    return active


def _occurrence(
    *,
    run: CollectorRun,
    checks: list[CheckResult],
    classification: FailureClassification,
    action: RecommendedAction,
    baseline: BaselineProfile | None,
    at: datetime,
) -> dict[str, Any]:
    section = orchestration(run)
    return {
        "detected_at": at.isoformat(),
        "collector_run_id": str(run.id),
        "run_status": run.status.value,
        "stage": section.get("stage"),
        "classification": classification.value,
        "recommended_action": action.value,
        "observed_record_count": observed_record_count(run),
        "baseline": baseline.model_dump(mode="json") if baseline else None,
        "checks": [check.model_dump(mode="json") for check in checks],
        # Bounded, verbatim. Never normalized, never repaired.
        "sample_violations": sample_violations(run),
    }


def _append_occurrence(
    evidence: dict[str, Any] | None, occurrence: dict[str, Any]
) -> dict[str, Any]:
    current = dict(evidence or {})
    occurrences = list(current.get("occurrences") or [])
    occurrences.append(occurrence)
    current["occurrences"] = occurrences[-MAX_RECORDED_OCCURRENCES:]
    return current


# -- healing lifecycle ------------------------------------------------------
# RecallGuard tracks the lifecycle; it does not perform repairs. Nothing
# below contacts Bright Data.


def start_healing(
    session: Session,
    incident: ReliabilityIncident,
    *,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityIncident:
    """DEGRADED -> HEALING, consuming one autonomous repair attempt.

    Refused unless the diagnosis actually calls for a scraper repair: a
    provider outage or an internal ingestion failure is not something
    healing the scraper can fix.

    A fourth attempt is refused and the incident is moved to
    MANUAL_REVIEW / ESCALATE.
    """
    if incident.status is not IncidentStatus.DEGRADED:
        raise IncidentTransitionError(
            f"incident {incident.id} is {incident.status.value}; healing can "
            "only start from degraded"
        )
    if incident.recommended_action not in _REPAIRABLE_ACTIONS:
        raise IncidentTransitionError(
            f"incident {incident.id} recommends "
            f"{incident.recommended_action.value}; healing is only appropriate "
            "for a repairable extraction failure"
        )
    if incident.repair_attempts >= MAX_AUTONOMOUS_REPAIR_ATTEMPTS:
        _escalate(session, incident, now=now)
        raise RepairAttemptLimitExceededError(
            f"incident {incident.id} has already used "
            f"{incident.repair_attempts} autonomous repair attempts; escalated "
            "to manual review"
        )

    incident.repair_attempts += 1
    incident.status = IncidentStatus.HEALING
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "healing_started",
            "at": now().isoformat(),
            "attempt": incident.repair_attempts,
        },
    )
    session.commit()
    session.refresh(incident)
    logger.info(
        "healing_started",
        extra={"incident_id": str(incident.id), "attempt": incident.repair_attempts},
    )
    return incident


# Statuses a repair cycle can be re-entered from. DEGRADED covers the
# normal case (a previous attempt ended and handed the incident back);
# the in-flight statuses cover a process that died mid-attempt and left
# the incident where it stood. MANUAL_REVIEW is deliberately absent: a
# human owns that incident. RECOVERED is absent because it is closed.
RESUMABLE_INCIDENT_STATUSES = frozenset(
    {
        IncidentStatus.DEGRADED,
        IncidentStatus.HEALING,
        IncidentStatus.VALIDATING,
        IncidentStatus.VERIFYING,
    }
)


def resume_healing(
    session: Session,
    incident: ReliabilityIncident,
    *,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityIncident:
    """Re-enter an attempt that is already in flight at the provider.

    The difference from start_healing is the whole point: this consumes
    NO autonomous repair attempt and applies no attempt-budget check,
    because nothing new is being started. Attempt 1 stays attempt 1 when
    GapRadar stops waiting, the process exits, or the next cron run picks
    the same repair back up.

    That is only sound because the caller has already established, from
    the provider itself, that a repair is genuinely still in flight --
    which is why this refuses an incident that has never had an attempt:
    with repair_attempts at zero there is nothing of ours to resume, and
    adopting a repair GapRadar did not start would silently take
    ownership of someone else's work.

    Refused for MANUAL_REVIEW (a human owns it) and RECOVERED (closed),
    and for a diagnosis that a scraper repair cannot address.
    """
    if incident.status not in RESUMABLE_INCIDENT_STATUSES:
        raise IncidentTransitionError(
            f"incident {incident.id} is {incident.status.value}; an in-flight "
            "repair can only be resumed from degraded or an in-flight status"
        )
    if incident.recommended_action not in _REPAIRABLE_ACTIONS:
        raise IncidentTransitionError(
            f"incident {incident.id} recommends "
            f"{incident.recommended_action.value}; healing is only appropriate "
            "for a repairable extraction failure"
        )
    if incident.repair_attempts < 1:
        raise IncidentTransitionError(
            f"incident {incident.id} has no repair attempt to resume"
        )

    resumed_from = incident.status
    incident.status = IncidentStatus.HEALING
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "healing_resumed",
            "at": now().isoformat(),
            "attempt": incident.repair_attempts,
            "resumed_from": resumed_from.value,
        },
    )
    session.commit()
    session.refresh(incident)
    logger.info(
        "healing_resumed",
        extra={
            "incident_id": str(incident.id),
            "attempt": incident.repair_attempts,
            "resumed_from": resumed_from.value,
        },
    )
    return incident


def register_repair_candidate(
    session: Session,
    incident: ReliabilityIncident,
    *,
    candidate: dict[str, Any] | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityIncident:
    """HEALING -> VALIDATING: a repair candidate exists.

    "Exists" is all this records. The candidate is not trusted, not
    applied, and emphatically not recovery -- it only makes the incident
    eligible for verification by a later independent run. `candidate`
    carries whatever the healer knows (Task 4 supplies real Bright Data
    metadata); nothing is invented to fill it.
    """
    if incident.status is not IncidentStatus.HEALING:
        raise IncidentTransitionError(
            f"incident {incident.id} is {incident.status.value}; a repair "
            "candidate can only be registered while healing"
        )

    registered_at = now()
    incident.status = IncidentStatus.VALIDATING
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "repair_candidate_registered",
            "at": registered_at.isoformat(),
            "attempt": incident.repair_attempts,
            "candidate": candidate or {},
        },
    )
    session.commit()
    session.refresh(incident)
    logger.info(
        "repair_candidate_registered",
        extra={"incident_id": str(incident.id), "attempt": incident.repair_attempts},
    )
    return incident


def begin_validation(
    session: Session,
    incident: ReliabilityIncident,
    *,
    verification_run: CollectorRun,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityIncident:
    """VALIDATING -> VERIFYING: a fresh run is available to judge.

    The run is checked for independence here, before any of its results
    are looked at, so a disqualified run can never influence the verdict.
    """
    if incident.status is not IncidentStatus.VALIDATING:
        raise IncidentTransitionError(
            f"incident {incident.id} is {incident.status.value}; verification "
            "can only begin from validating"
        )
    _require_independent_run(incident, verification_run)

    incident.status = IncidentStatus.VERIFYING
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "verification_started",
            "at": now().isoformat(),
            "collector_run_id": str(verification_run.id),
        },
    )
    session.commit()
    session.refresh(incident)
    return incident


def verify_recovery(
    session: Session,
    incident: ReliabilityIncident,
    *,
    verification_run: CollectorRun,
    baseline: BaselineProfile | None = None,
    policy: ReliabilityPolicy = DEFAULT_POLICY,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityEvaluation:
    """Judge a repair by an independent collection -- the core rule.

    A repair candidate cannot verify itself and approval proves nothing.
    Recovery requires a NEW run of the same collector, started after the
    repair candidate was registered, never used to verify this incident
    before, that executed successfully AND passes every reliability check
    (transport, source contract, semantics, completeness, ingestion).

    Pass -> RECOVERED with a structured proof. Fail -> back to DEGRADED,
    keeping the failed run id, the failed checks, and the attempt count.
    """
    if incident.status is IncidentStatus.VALIDATING:
        begin_validation(session, incident, verification_run=verification_run, now=now)
    elif incident.status is IncidentStatus.VERIFYING:
        _require_independent_run(incident, verification_run)
    else:
        raise IncidentTransitionError(
            f"incident {incident.id} is {incident.status.value}; recovery can "
            "only be verified while validating or verifying"
        )

    if verification_run.status is not RunStatus.SUCCEEDED:
        raise VerificationRunRejectedError(
            f"verification run {verification_run.id} is "
            f"{verification_run.status.value}; recovery requires a run that "
            "executed successfully"
        )

    checks = evaluate_run_checks(verification_run, baseline=baseline, policy=policy)
    passed = all(check.passed for check in checks)
    at = now()

    if not passed:
        return _fail_verification(
            session, incident, verification_run, checks=checks, at=at
        )
    return _record_recovery(session, incident, verification_run, checks=checks, at=at)


def _require_independent_run(
    incident: ReliabilityIncident, verification_run: CollectorRun
) -> None:
    if verification_run.collector_id != incident.collector_id:
        raise VerificationRunRejectedError(
            f"run {verification_run.id} belongs to another collector"
        )
    if verification_run.id == incident.detection_run_id:
        raise VerificationRunRejectedError(
            f"run {verification_run.id} is the run that detected this "
            "incident; recovery requires a fresh collection"
        )
    already_used = _verification_run_ids(incident)
    if str(verification_run.id) in already_used:
        raise VerificationRunRejectedError(
            f"run {verification_run.id} has already been used to verify this incident"
        )

    repair_started_at = _repair_candidate_registered_at(incident)
    if repair_started_at is None:
        raise VerificationRunRejectedError(
            f"incident {incident.id} has no registered repair candidate to "
            "verify against"
        )
    run_started_at = _as_utc(verification_run.started_at or verification_run.created_at)
    if run_started_at is None or run_started_at <= _as_utc(repair_started_at):
        raise VerificationRunRejectedError(
            f"run {verification_run.id} did not start after the repair "
            "candidate was registered; a pre-repair collection cannot prove "
            "recovery"
        )


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize one of our own timestamps to an aware UTC datetime.

    Every timestamp compared here is written by GapRadar in UTC into a
    DateTime(timezone=True) column, but some database backends (SQLite,
    used by the test suite) hand it back without the offset. Attaching
    UTC to a value we wrote as UTC is safe; provider-supplied timestamps
    are never treated this leniently -- app.ingestion.normalizer rejects
    naive ones outright.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _verification_run_ids(incident: ReliabilityIncident) -> set[str]:
    events = (incident.evidence or {}).get("events") or []
    return {
        event["collector_run_id"]
        for event in events
        if isinstance(event, dict)
        and event.get("event") in ("verification_failed", "verification_passed")
        and isinstance(event.get("collector_run_id"), str)
    }


def _repair_candidate_registered_at(
    incident: ReliabilityIncident,
) -> datetime | None:
    events = (incident.evidence or {}).get("events") or []
    stamps = [
        datetime.fromisoformat(event["at"])
        for event in events
        if isinstance(event, dict)
        and event.get("event") == "repair_candidate_registered"
        and isinstance(event.get("at"), str)
    ]
    return max(stamps) if stamps else None


def _fail_verification(
    session: Session,
    incident: ReliabilityIncident,
    verification_run: CollectorRun,
    *,
    checks: list[CheckResult],
    at: datetime,
) -> ReliabilityEvaluation:
    failed = [check for check in checks if not check.passed]
    incident.status = IncidentStatus.DEGRADED
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "verification_failed",
            "at": at.isoformat(),
            "collector_run_id": str(verification_run.id),
            "attempt": incident.repair_attempts,
            "failed_checks": [check.model_dump(mode="json") for check in failed],
            "sample_violations": sample_violations(verification_run),
        },
    )
    session.commit()
    session.refresh(incident)

    logger.warning(
        "recovery_verification_failed",
        extra={
            "incident_id": str(incident.id),
            "collector_run_id": str(verification_run.id),
            "attempt": incident.repair_attempts,
        },
    )
    return ReliabilityEvaluation(
        collector_run_id=verification_run.id,
        passed=False,
        state=_state_for(incident),
        classification=incident.classification,
        recommended_action=incident.recommended_action,
        checks=checks,
        incident_id=incident.id,
    )


def _record_recovery(
    session: Session,
    incident: ReliabilityIncident,
    verification_run: CollectorRun,
    *,
    checks: list[CheckResult],
    at: datetime,
) -> ReliabilityEvaluation:
    proof = RecoveryProof(
        incident_id=incident.id,
        collector_id=incident.collector_id,
        detection_run_id=incident.detection_run_id,
        verification_run_id=verification_run.id,
        classification=incident.classification,
        repair_attempt=incident.repair_attempts,
        checks=checks,
        verified_at=at,
    )
    incident.status = IncidentStatus.RECOVERED
    incident.recovered_at = at
    incident.verification_run_id = verification_run.id
    incident.recovery_proof = proof.model_dump(mode="json")
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "verification_passed",
            "at": at.isoformat(),
            "collector_run_id": str(verification_run.id),
            "attempt": incident.repair_attempts,
        },
    )
    session.commit()
    session.refresh(incident)

    logger.info(
        "recovery_verified",
        extra={
            "incident_id": str(incident.id),
            "detection_run_id": str(incident.detection_run_id),
            "verification_run_id": str(verification_run.id),
            "attempt": incident.repair_attempts,
        },
    )
    return ReliabilityEvaluation(
        collector_run_id=verification_run.id,
        passed=True,
        # The incident is closed, so the collector is healthy again --
        # unless something else is still open against it.
        state=collector_reliability_state(session, collector_id=incident.collector_id),
        classification=incident.classification,
        checks=checks,
        incident_id=incident.id,
    )


def record_healing_failure(
    session: Session,
    incident: ReliabilityIncident,
    *,
    reason: str,
    evidence: dict[str, Any] | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityIncident:
    """Return an in-flight repair to DEGRADED, keeping why it stopped.

    Used when a repair attempt ends without ever reaching an independent
    verification -- the provider failed, the candidate was rejected, or
    the fresh collection never completed. The attempt still counts: the
    attempt budget exists to stop repeated futile repairs, and a repair
    that fell over is exactly that.
    """
    if incident.status not in (
        IncidentStatus.HEALING,
        IncidentStatus.VALIDATING,
        IncidentStatus.VERIFYING,
    ):
        raise IncidentTransitionError(
            f"incident {incident.id} is {incident.status.value}; only an "
            "in-flight repair can be recorded as failed"
        )

    incident.status = IncidentStatus.DEGRADED
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "healing_failed",
            "at": now().isoformat(),
            "attempt": incident.repair_attempts,
            "reason": reason,
            **(evidence or {}),
        },
    )
    session.commit()
    session.refresh(incident)
    logger.warning(
        "healing_failed",
        extra={
            "incident_id": str(incident.id),
            "attempt": incident.repair_attempts,
            "reason": reason,
        },
    )
    return incident


def escalate(
    session: Session,
    incident: ReliabilityIncident,
    *,
    reason: str,
    evidence: dict[str, Any] | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> ReliabilityIncident:
    """Hand the incident to a human.

    Called when RecallGuard cannot decide safely on its own -- not only
    when the attempt budget runs out. An escalated incident stays active
    and is never autonomously repaired again.
    """
    _escalate(session, incident, now=now)
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "escalation_reason",
            "at": now().isoformat(),
            "reason": reason,
            **(evidence or {}),
        },
    )
    session.commit()
    session.refresh(incident)
    return incident


def _escalate(
    session: Session,
    incident: ReliabilityIncident,
    *,
    now: Callable[[], datetime],
) -> None:
    incident.status = IncidentStatus.MANUAL_REVIEW
    incident.recommended_action = RecommendedAction.ESCALATE
    incident.evidence = _record_event(
        incident.evidence,
        {
            "event": "escalated_to_manual_review",
            "at": now().isoformat(),
            "attempts": incident.repair_attempts,
        },
    )
    session.commit()
    session.refresh(incident)
    logger.warning(
        "incident_escalated",
        extra={"incident_id": str(incident.id), "attempts": incident.repair_attempts},
    )


def _record_event(
    evidence: dict[str, Any] | None, event: dict[str, Any]
) -> dict[str, Any]:
    current = dict(evidence or {})
    events = list(current.get("events") or [])
    events.append(event)
    current["events"] = events
    return current
