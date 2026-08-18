"""Rejoining a repair Bright Data is already running.

The production failure these tests exist for: a real repair
(incident ae20c718-55b9-4fa3-9bd9-31b78f23495e, collector
c_msya3ha629w2q9c62m) was still actively working after GapRadar's local
900s budget elapsed. The old flow recorded that as a provider failure and
would have posted refactor_template a second time on the next
invocation, on top of the live repair.

Every provider call here goes through an httpx.MockTransport.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.collection.schemas import PollingPolicy
from app.config import Settings
from app.db.models import Collector, ReliabilityIncident
from app.domain.enums import IncidentStatus, RecommendedAction
from app.integrations.brightdata.schemas import HealingStatus
from app.recallguard.healing import (
    HealingAttemptResult,
    HealingOutcome,
    SelfHealingPolicy,
    resume_or_execute_healing_attempt,
)
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    evaluate_collector_run,
    record_healing_failure,
    start_healing,
)
from tests.integrations.brightdata.conftest import make_client
from tests.recallguard.conftest import FakeClock, RunBuilder, invalid_record
from tests.recallguard.healing_fakes import (
    HealClock,
    ScriptedProvider,
    awaiting_approval,
    awaiting_approval_without_preview,
    done,
    failed,
    provider_running,
    running,
)

BASELINE = BaselineProfile(label="fix_my_itch_healthy_v1", record_count=10)
FAST_HEALING = SelfHealingPolicy(interval_seconds=1.0, timeout_seconds=60.0)
IMPATIENT = SelfHealingPolicy(interval_seconds=2.0, timeout_seconds=6.0)
FAST_COLLECTION = PollingPolicy(interval_seconds=1.0, timeout_seconds=60.0)


def good_records(count: int, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in records[:count]]


@pytest.fixture
def degraded_incident(db_session: Session, runs: RunBuilder) -> ReliabilityIncident:
    """A collector degraded by semantic drift, with no attempt started."""
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=10
        ),
        baseline=BASELINE,
        now=FakeClock(),
    )
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None
    return incident


@pytest.fixture
def timed_out_incident(
    db_session: Session, degraded_incident: ReliabilityIncident
) -> ReliabilityIncident:
    """Exactly the production state: attempt 1 started, GapRadar gave up.

    The incident is back at DEGRADED with repair_attempts=1 while the
    provider's repair carries on -- which is the whole reason a naive
    second invocation used to trigger a duplicate.
    """
    clock = FakeClock()
    start_healing(db_session, degraded_incident, now=clock)
    record_healing_failure(
        db_session,
        degraded_incident,
        reason="local_polling_timeout",
        evidence={"attempt": 1, "provider_failed": False},
        now=clock,
    )
    assert degraded_incident.status is IncidentStatus.DEGRADED
    assert degraded_incident.repair_attempts == 1
    return degraded_incident


def resume_or_heal(
    db_session: Session,
    provider: ScriptedProvider,
    incident: ReliabilityIncident,
    collector: Collector,
    settings: Settings,
    *,
    clock: HealClock | None = None,
    policy: SelfHealingPolicy = FAST_HEALING,
) -> HealingAttemptResult:
    clock = clock or HealClock()
    with make_client(settings, provider) as client:
        return resume_or_execute_healing_attempt(
            db_session,
            client,
            incident=incident,
            collector=collector,
            baseline=BASELINE,
            healing_policy=policy,
            collection_polling=FAST_COLLECTION,
            now=clock.now,
            sleep=clock.sleep,
        )


# --- 1. a repair still running is resumed, never re-triggered --------------


def test_a_running_provider_repair_is_resumed_without_a_second_trigger(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    provider = ScriptedProvider(progress=[provider_running()], repair_in_flight=True)

    result = resume_or_heal(
        db_session,
        provider,
        timed_out_incident,
        collector,
        brightdata_settings,
        policy=IMPATIENT,
    )

    # No new repair was requested, and attempt 1 is still attempt 1.
    assert provider.heal_requests == []
    assert result.attempt == 1
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 1
    # Stopping again is a local timeout, not a provider failure.
    assert result.outcome is HealingOutcome.LOCAL_TIMEOUT
    assert result.provider_status is HealingStatus.RUNNING
    assert result.recovered is False
    assert timed_out_incident.status is IncidentStatus.DEGRADED
    assert any(
        event["event"] == "healing_resumed"
        for event in timed_out_incident.evidence["events"]
    )


# --- 2. a repair waiting at the gate continues the candidate path ----------


def test_a_repair_awaiting_approval_resumes_the_candidate_path(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    preview = good_records(3, healthy_records)
    provider = ScriptedProvider(
        progress=[awaiting_approval(preview), done()],
        dataset=good_records(10, healthy_records),
        repair_in_flight=True,
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert provider.heal_requests == []
    assert result.attempt == 1
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 1
    # The candidate was registered and judged on this invocation.
    events = [event["event"] for event in timed_out_incident.evidence["events"]]
    assert "healing_resumed" in events
    assert "repair_candidate_registered" in events


# --- 3. nothing in flight: the ordinary new attempt still happens ----------


def test_with_no_provider_repair_a_new_attempt_starts_normally(
    db_session: Session,
    collector: Collector,
    degraded_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    """The progress endpoint has no job to report until one is triggered."""
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )

    result = resume_or_heal(
        db_session, provider, degraded_incident, collector, brightdata_settings
    )

    assert len(provider.heal_requests) == 1
    assert result.attempt == 1
    db_session.refresh(degraded_incident)
    assert degraded_incident.repair_attempts == 1


def test_a_terminal_status_from_a_past_repair_does_not_block_a_new_attempt(
    db_session: Session,
    collector: Collector,
    degraded_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    """DONE is the leftover state of a finished repair, not a live one.

    Treating it as in-flight would deadlock the collector forever.
    """
    provider = ScriptedProvider(
        progress=[done(), awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
        repair_in_flight=True,
    )

    resume_or_heal(
        db_session, provider, degraded_incident, collector, brightdata_settings
    )

    assert len(provider.heal_requests) == 1
    db_session.refresh(degraded_incident)
    assert degraded_incident.repair_attempts == 1


# --- 4 & 5. a resumed repair still has to earn recovery --------------------


def test_a_resumed_repair_reaches_the_gate_and_is_proven_by_a_fresh_run(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    """Resume skips the trigger. It skips no gate.

    Preflight, approval, a fresh production collection and verify_recovery
    all still happen, and the proof records the original attempt.
    """
    preview = good_records(3, healthy_records)
    provider = ScriptedProvider(
        progress=[provider_running(), awaiting_approval(preview), done()],
        dataset=good_records(10, healthy_records),
        repair_in_flight=True,
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert provider.heal_requests == []
    assert result.outcome is HealingOutcome.RECOVERED
    assert result.recovered is True
    assert result.candidate_approved is True
    # Approval happened programmatically, after preflight passed.
    assert provider.resume_bodies == [{"message": True, "auto_save": True}]
    # A real production collection ran to prove it.
    assert len(provider.collection_triggers) == 1

    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.RECOVERED
    assert timed_out_incident.repair_attempts == 1
    proof = timed_out_incident.recovery_proof
    assert proof is not None
    assert proof["repair_attempt"] == 1
    assert proof["verification_run_id"] == str(result.verification_run_id)
    assert proof["verification_run_id"] != proof["detection_run_id"]


def test_a_resumed_repair_whose_preview_is_still_broken_is_rejected(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    """Resuming grants no leniency: one bad preview record still rejects."""
    broken = good_records(3, healthy_records)
    broken[0] = {**broken[0], "tam_score": 60}
    provider = ScriptedProvider(
        progress=[awaiting_approval(broken)], repair_in_flight=True
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.CANDIDATE_REJECTED
    assert result.candidate_approved is False
    assert provider.resume_bodies == [{"message": False, "auto_save": False}]
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.DEGRADED
    assert timed_out_incident.recovery_proof is None


def test_provider_done_alone_never_recovers_a_resumed_incident(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Bright Data DONE is not RecallGuard HEALTHY, resumed or not."""
    provider = ScriptedProvider(
        progress=[provider_running(), done()], repair_in_flight=True
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.recovered is False
    assert result.outcome is HealingOutcome.PROVIDER_FAILED
    assert provider.resume_requests == []
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.DEGRADED
    assert timed_out_incident.recovery_proof is None


# --- 6. a genuine provider failure -----------------------------------------


def test_a_provider_failure_is_recorded_and_a_later_attempt_may_begin(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[
            failed(),
            awaiting_approval(good_records(3, healthy_records)),
            done(),
        ],
        dataset=good_records(10, healthy_records),
        repair_in_flight=True,
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    # The failed repair is history, so a genuinely new attempt starts.
    assert len(provider.heal_requests) == 1
    assert result.attempt == 2
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 2


def test_an_incident_stuck_in_flight_is_released_when_the_repair_is_gone(
    db_session: Session,
    collector: Collector,
    degraded_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """A process killed mid-attempt leaves HEALING behind.

    The provider is the authority: it reports the repair as failed, so
    the attempt is recorded as ended and the incident is handed back as
    DEGRADED for a later attempt. No repair is triggered in the same
    breath.
    """
    start_healing(db_session, degraded_incident, now=FakeClock())
    assert degraded_incident.status is IncidentStatus.HEALING
    provider = ScriptedProvider(progress=[failed()], repair_in_flight=True)

    result = resume_or_heal(
        db_session, provider, degraded_incident, collector, brightdata_settings
    )

    assert provider.heal_requests == []
    assert result.outcome is HealingOutcome.PROVIDER_FAILED
    db_session.refresh(degraded_incident)
    assert degraded_incident.status is IncidentStatus.DEGRADED
    assert degraded_incident.repair_attempts == 1


# --- 7 & 8. restarts never duplicate a repair ------------------------------


def test_repeated_invocations_never_post_refactor_template_twice(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    """Three invocations across two local timeouts and one restart.

    The provider works, GapRadar gives up twice, and the third invocation
    finds the candidate waiting. One repair, one attempt, no duplicates.
    """
    provider = ScriptedProvider(progress=[provider_running()], repair_in_flight=True)

    first = resume_or_heal(
        db_session,
        provider,
        timed_out_incident,
        collector,
        brightdata_settings,
        policy=IMPATIENT,
    )
    second = resume_or_heal(
        db_session,
        provider,
        timed_out_incident,
        collector,
        brightdata_settings,
        policy=IMPATIENT,
    )

    assert first.outcome is HealingOutcome.LOCAL_TIMEOUT
    assert second.outcome is HealingOutcome.LOCAL_TIMEOUT
    assert provider.heal_requests == []
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 1

    # The provider finally reaches the gate; the same attempt finishes it.
    provider.progress = [awaiting_approval(good_records(3, healthy_records)), done()]
    provider.progress_index = 0
    provider.dataset = good_records(10, healthy_records)

    third = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert provider.heal_requests == []
    assert third.recovered is True
    assert third.attempt == 1
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 1


# --- 9. a rejected candidate is an attempt, not an escalation --------------


def test_a_candidate_with_no_preview_is_rejected_and_the_next_call_retries(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    """The production sequence of ae20c718, invocation by invocation.

    Attempt 1 reached user_approval with a diff and no preview_result.
    RecallGuard rejects it, which is where the old flow instead escalated
    to MANUAL_REVIEW and stranded the incident for good. The provider
    then reports the rejected job as failed, and the next invocation must
    read that as "nothing in flight" and start a genuine attempt 2.
    """
    rejecting = ScriptedProvider(
        progress=[awaiting_approval_without_preview()], repair_in_flight=True
    )

    first = resume_or_heal(
        db_session, rejecting, timed_out_incident, collector, brightdata_settings
    )

    assert first.outcome is HealingOutcome.CANDIDATE_REJECTED
    assert first.candidate_approved is False
    assert first.attempt == 1
    # Resuming triggers nothing, and the gate is closed by a reject.
    assert rejecting.heal_requests == []
    assert rejecting.resume_bodies == [{"message": False, "auto_save": False}]

    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.DEGRADED
    assert timed_out_incident.recommended_action is RecommendedAction.REQUEST_HEAL
    assert timed_out_incident.repair_attempts == 1

    # Next invocation: the rejected job is reported failed, exactly as
    # the provider reported it in production (status=failed,
    # step=user_approval).
    retrying = ScriptedProvider(
        progress=[
            {"status": "failed", "step": "user_approval"},
            awaiting_approval(good_records(3, healthy_records)),
            done(),
        ],
        dataset=good_records(10, healthy_records),
        repair_in_flight=True,
    )

    second = resume_or_heal(
        db_session, retrying, timed_out_incident, collector, brightdata_settings
    )

    # A genuinely new attempt: start_healing bumped 1 -> 2, and
    # refactor_template was posted exactly once.
    assert second.attempt == 2
    assert len(retrying.heal_requests) == 1
    assert second.recovered is True
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 2


def test_a_rejected_candidate_does_not_escalate_before_the_budget_is_gone(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Attempt 2 of 3 rejected: still DEGRADED, still a human's day off."""
    timed_out_incident.repair_attempts = 1
    db_session.commit()
    provider = ScriptedProvider(
        progress=[awaiting_approval_without_preview()], repair_in_flight=True
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.CANDIDATE_REJECTED
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.DEGRADED
    assert timed_out_incident.recommended_action is RecommendedAction.REQUEST_HEAL


def test_the_last_rejection_in_the_budget_escalates_after_rejecting(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Resuming attempt 3 spends the budget: reject first, then a human."""
    timed_out_incident.repair_attempts = 3
    db_session.commit()
    provider = ScriptedProvider(
        progress=[awaiting_approval_without_preview()], repair_in_flight=True
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.ESCALATED
    assert result.candidate_approved is False
    # The gate is closed before the incident is handed over.
    assert provider.resume_bodies == [{"message": False, "auto_save": False}]
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.MANUAL_REVIEW
    assert timed_out_incident.recommended_action is RecommendedAction.ESCALATE
    assert timed_out_incident.repair_attempts == 3


def test_an_escalated_incident_is_left_alone_without_raising(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Once a human owns it, further invocations are a plain no-op."""
    timed_out_incident.status = IncidentStatus.MANUAL_REVIEW
    timed_out_incident.recommended_action = RecommendedAction.ESCALATE
    db_session.commit()
    provider = ScriptedProvider(
        progress=[awaiting_approval_without_preview()], repair_in_flight=True
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.ESCALATED
    assert provider.requests == []
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.MANUAL_REVIEW
    assert timed_out_incident.repair_attempts == 1


# --- fail-closed refusals ---------------------------------------------------


def test_an_unreadable_provider_status_triggers_nothing(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Not knowing is not permission to start work."""
    provider = ScriptedProvider(
        progress_response=httpx.Response(503, json={"error": "unavailable"}),
        repair_in_flight=True,
    )

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.REFUSED
    assert provider.heal_requests == []
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.DEGRADED
    assert timed_out_incident.repair_attempts == 1


def test_an_unrecognized_provider_status_triggers_nothing(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """An unknown status might be a live repair under a new name."""
    provider = ScriptedProvider(progress=[running()], repair_in_flight=True)

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.REFUSED
    assert result.provider_status is HealingStatus.UNKNOWN
    assert provider.heal_requests == []
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.repair_attempts == 1


def test_a_repair_gapradar_never_started_is_not_adopted(
    db_session: Session,
    collector: Collector,
    degraded_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Someone else's in-flight repair: neither adopted nor duplicated."""
    provider = ScriptedProvider(progress=[provider_running()], repair_in_flight=True)

    result = resume_or_heal(
        db_session, provider, degraded_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.REFUSED
    assert provider.heal_requests == []
    db_session.refresh(degraded_incident)
    assert degraded_incident.repair_attempts == 0
    assert degraded_incident.status is IncidentStatus.DEGRADED


def test_an_outage_still_never_reaches_the_provider_at_all(
    db_session: Session,
    collector: Collector,
    runs: RunBuilder,
    brightdata_settings: Settings,
) -> None:
    """A non-repairable diagnosis is refused before any call is made."""
    from app.recallguard.errors import IncidentTransitionError

    evaluation = evaluate_collector_run(
        db_session, run=runs.failed("timeout"), baseline=BASELINE, now=FakeClock()
    )
    outage = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert outage is not None
    provider = ScriptedProvider(progress=[provider_running()], repair_in_flight=True)

    with pytest.raises(IncidentTransitionError):
        resume_or_heal(db_session, provider, outage, collector, brightdata_settings)

    assert provider.requests == []


def test_the_three_attempt_budget_still_applies_to_new_attempts(
    db_session: Session,
    collector: Collector,
    timed_out_incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """Resuming consumes no attempt; starting one still does."""
    timed_out_incident.repair_attempts = 3
    db_session.commit()
    provider = ScriptedProvider(progress=[done()], repair_in_flight=True)

    result = resume_or_heal(
        db_session, provider, timed_out_incident, collector, brightdata_settings
    )

    assert result.outcome is HealingOutcome.ESCALATED
    assert provider.heal_requests == []
    db_session.refresh(timed_out_incident)
    assert timed_out_incident.status is IncidentStatus.MANUAL_REVIEW
    assert timed_out_incident.repair_attempts == 3
