"""The full self-healing loop: propose, gate, approve, prove."""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collection.schemas import PollingPolicy
from app.config import Settings
from app.db.models import Collector, CollectorRun, ReliabilityIncident, Signal
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
)
from app.integrations.brightdata.schemas import HealingStatus
from app.recallguard.errors import IncidentTransitionError
from app.recallguard.healing import (
    HealingAttemptResult,
    HealingOutcome,
    SelfHealingPolicy,
    execute_healing_attempt,
)
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    MAX_AUTONOMOUS_REPAIR_ATTEMPTS,
    collector_reliability_state,
    evaluate_collector_run,
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
FAST_COLLECTION = PollingPolicy(interval_seconds=1.0, timeout_seconds=60.0)


def good_records(count: int, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in records[:count]]


def broken_record(records: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    record = dict(records[0])
    record.update(overrides)
    return record


@pytest.fixture
def incident(db_session: Session, runs: RunBuilder) -> ReliabilityIncident:
    """A collector degraded by semantic drift, awaiting repair."""
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=10
        ),
        baseline=BASELINE,
        now=FakeClock(),
    )
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    found = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert found is not None
    return found


def heal(
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
        return execute_healing_attempt(
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


def signal_count(db_session: Session) -> int:
    return db_session.execute(select(func.count()).select_from(Signal)).scalar_one()


# --- lifecycle guards ------------------------------------------------------


def test_a_provider_outage_never_reaches_the_self_heal_endpoint(
    db_session: Session,
    collector: Collector,
    runs: RunBuilder,
    brightdata_settings: Settings,
) -> None:
    evaluation = evaluate_collector_run(
        db_session, run=runs.failed("timeout"), baseline=BASELINE, now=FakeClock()
    )
    outage = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert outage is not None
    assert outage.classification is FailureClassification.OUTAGE
    provider = ScriptedProvider(progress=[done()])

    with pytest.raises(IncidentTransitionError):
        heal(db_session, provider, outage, collector, brightdata_settings)

    assert provider.requests == []


def test_a_recovered_incident_cannot_be_healed_again(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )
    result = heal(db_session, provider, incident, collector, brightdata_settings)
    assert result.recovered is True

    with pytest.raises(IncidentTransitionError):
        heal(db_session, provider, incident, collector, brightdata_settings)


def test_drift_starts_healing_and_consumes_exactly_one_attempt(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[
            running(),
            awaiting_approval(good_records(2, healthy_records)),
            done(),
        ],
        dataset=good_records(10, healthy_records),
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    db_session.refresh(incident)
    assert incident.repair_attempts == 1
    assert result.attempt == 1
    assert len(provider.heal_requests) == 1
    body = json.loads(provider.heal_requests[0].content)
    assert "tam_score returned 60" in body["prompt"]
    assert body["custom_input"] == []


def test_the_provider_candidate_is_registered_with_bounded_evidence(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    preview = good_records(3, healthy_records)
    provider = ScriptedProvider(
        progress=[awaiting_approval(preview), done()],
        dataset=good_records(10, healthy_records),
    )

    heal(db_session, provider, incident, collector, brightdata_settings)

    db_session.refresh(incident)
    registered = next(
        event
        for event in incident.evidence["events"]
        if event["event"] == "repair_candidate_registered"
    )
    assert registered["candidate"]["provider_status"] == "awaiting_approval"
    assert registered["candidate"]["preview_records"] == 3
    assert registered["candidate"]["step"] == "review_diff"
    # A summary, not a second copy of the preview payload.
    assert "preview_result" not in registered["candidate"]


# --- candidate preflight ---------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"tam_score": 60},
        {"confidence_score": 4},
        {"problem": ""},
        {"source_url": "https://evil.example.com/"},
        {"severity_score": 42},
    ],
)
def test_a_candidate_whose_preview_violates_the_contract_is_rejected(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
    overrides: dict[str, Any],
) -> None:
    preview = [
        *good_records(2, healthy_records),
        broken_record(healthy_records, **overrides),
    ]
    provider = ScriptedProvider(progress=[awaiting_approval(preview)])

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.CANDIDATE_REJECTED
    assert result.recovered is False
    assert result.candidate_approved is False
    assert provider.resume_bodies == [{"message": False, "auto_save": False}]
    # No collection was attempted -- nothing was deployed to verify.
    assert provider.collection_triggers == []
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    # A rejection spends the attempt and nothing else: the diagnosis and
    # the recommendation both survive, so attempt 2 may still be tried.
    assert incident.classification is FailureClassification.EXTRACTION_DRIFT
    assert incident.recommended_action is RecommendedAction.REQUEST_HEAL
    assert incident.repair_attempts == 1
    assert incident.recovery_proof is None


def test_a_rejected_candidate_keeps_the_offending_preview_values(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[awaiting_approval([broken_record(healthy_records, tam_score=60)])]
    )

    heal(db_session, provider, incident, collector, brightdata_settings)

    db_session.refresh(incident)
    failure = incident.evidence["events"][-1]
    assert failure["reason"] == "candidate_rejected"
    violation = failure["preflight"]["violations"][0]
    assert violation["raw"]["tam_score"] == 60


def test_a_healthy_preview_is_approved_with_auto_save(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )

    heal(db_session, provider, incident, collector, brightdata_settings)

    assert provider.resume_bodies == [{"message": True, "auto_save": True}]


def test_a_candidate_without_a_preview_is_rejected_not_escalated(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """The production failure this fix exists for.

    Incident ae20c718-55b9-4fa3-9bd9-31b78f23495e reached user_approval
    with a template_a/template_b diff and no preview_result. Nothing to
    validate means no basis to approve -- but that is one bad candidate
    on attempt 1 of 3, not a reason to wake a human. The gate is closed
    with an explicit reject and the incident stays repairable.
    """
    provider = ScriptedProvider(progress=[awaiting_approval_without_preview()])

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.CANDIDATE_REJECTED
    assert result.candidate_approved is False
    assert result.recovered is False
    # Exactly one gate call, and it is a reject: never an approval.
    assert provider.resume_bodies == [{"message": False, "auto_save": False}]
    # Nothing was deployed, so there is nothing to verify.
    assert provider.collection_triggers == []

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.classification is FailureClassification.EXTRACTION_DRIFT
    assert incident.recommended_action is RecommendedAction.REQUEST_HEAL
    assert incident.repair_attempts == 1
    assert incident.recovery_proof is None


def test_a_rejection_for_a_missing_preview_records_why(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """An operator must be able to read what was wrong with the candidate."""
    provider = ScriptedProvider(progress=[awaiting_approval_without_preview()])

    heal(db_session, provider, incident, collector, brightdata_settings)

    db_session.refresh(incident)
    failure = incident.evidence["events"][-1]
    assert failure["event"] == "healing_failed"
    assert failure["reason"] == "candidate_rejected"
    assert "no preview_result" in failure["preflight_reason"]
    assert failure["provider_rejected"] is True
    # The diff alone is recorded as what the provider did offer.
    assert failure["preflight"] == {"preview_records": 0, "has_diff": True}


def test_a_missing_preview_never_reaches_approval_even_if_reject_fails(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """A provider that will not take the rejection still gets no approval."""
    provider = ScriptedProvider(
        progress=[awaiting_approval_without_preview()],
        resume_response=httpx.Response(503, json={"error": "unavailable"}),
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.CANDIDATE_REJECTED
    assert result.candidate_approved is False
    # One attempt to close the gate, and it was the reject.
    assert provider.resume_bodies == [{"message": False, "auto_save": False}]
    assert provider.collection_triggers == []
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.evidence["events"][-1]["provider_rejected"] is False


# --- approval is not recovery ----------------------------------------------


def test_approval_alone_does_not_recover_the_incident(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    # The provider approves and completes, but the fresh collection comes
    # back empty: deployed is not proven.
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=[],
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert provider.resume_bodies == [{"message": True, "auto_save": True}]
    assert result.candidate_approved is True
    assert result.recovered is False
    assert result.outcome is HealingOutcome.VERIFICATION_FAILED
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovery_proof is None


def test_a_self_heal_failure_after_approval_returns_to_degraded(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), failed()]
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.PROVIDER_FAILED
    assert result.recovered is False
    assert provider.collection_triggers == []
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED


def test_a_self_heal_job_that_fails_before_the_gate_never_approves(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    provider = ScriptedProvider(progress=[running(), failed()])

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.PROVIDER_FAILED
    assert provider.resume_requests == []
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.repair_attempts == 1


def test_a_local_self_heal_timeout_is_not_a_provider_failure(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    """GapRadar stopped waiting. Bright Data did not fail.

    Reporting this as PROVIDER_FAILED is what let a live repair be
    mistaken for a dead one, so the two are now distinct outcomes. The
    incident goes back to DEGRADED, which is exactly what makes the same
    repair resumable on the next invocation.
    """
    provider = ScriptedProvider(progress=[provider_running()])

    result = heal(
        db_session,
        provider,
        incident,
        collector,
        brightdata_settings,
        policy=SelfHealingPolicy(interval_seconds=2.0, timeout_seconds=6.0),
    )

    assert result.outcome is HealingOutcome.LOCAL_TIMEOUT
    assert result.provider_status is HealingStatus.RUNNING
    assert result.recovered is False
    assert provider.resume_requests == []
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.repair_attempts == 1
    timeout_event = incident.evidence["events"][-1]
    assert timeout_event["reason"] == "local_polling_timeout"
    assert timeout_event["provider_failed"] is False
    # The local budget is never handed to the provider.
    for request in provider.requests:
        assert "deadline" not in request.url.params


# --- fresh production verification -----------------------------------------


def test_an_approved_repair_is_proven_by_a_fresh_production_collection(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    detection_run_id = incident.detection_run_id
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    # A real production trigger, on the same collector, with no dev
    # version and no provider-side deadline.
    trigger = provider.collection_triggers[0]
    assert trigger.url.params["collector"] == collector.external_collector_id
    assert set(trigger.url.params) == {"collector", "queue_next"}
    # A genuinely new run, distinct from the one that detected the fault.
    assert result.verification_run_id is not None
    assert result.verification_run_id != detection_run_id
    # And the signals it collected were really ingested.
    assert signal_count(db_session) == 10
    assert result.recovered is True


def test_a_fresh_run_that_still_drifts_leaves_the_incident_degraded(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    # The preview looked clean, but production still returns a bad value.
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=[
            *good_records(2, healthy_records),
            broken_record(healthy_records, tam_score=60),
        ],
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.recovered is False
    assert result.outcome is HealingOutcome.VERIFICATION_FAILED
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovery_proof is None
    # The already-approved candidate is never un-approved: one approval,
    # and no reject afterwards.
    assert provider.resume_bodies == [{"message": True, "auto_save": True}]
    # Nothing from the drifted verification collection was ingested.
    assert signal_count(db_session) == 0


def test_recovery_proof_names_the_detection_and_verification_runs(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    detection_run_id = incident.detection_run_id
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    db_session.refresh(incident)
    assert incident.status is IncidentStatus.RECOVERED
    proof = incident.recovery_proof
    assert proof["detection_run_id"] == str(detection_run_id)
    assert proof["verification_run_id"] == str(result.verification_run_id)
    assert proof["repair_attempt"] == 1
    assert proof["result"] == "pass"
    verification_run = db_session.get(CollectorRun, result.verification_run_id)
    assert verification_run is not None
    assert verification_run.collector_id == collector.id
    assert (
        collector_reliability_state(db_session, collector_id=collector.id)
        is ReliabilityState.HEALTHY
    )


# --- retries and the attempt budget ----------------------------------------


def test_a_failed_attempt_allows_another(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    rejecting = ScriptedProvider(
        progress=[awaiting_approval([broken_record(healthy_records, tam_score=60)])]
    )
    first = heal(db_session, rejecting, incident, collector, brightdata_settings)
    assert first.outcome is HealingOutcome.CANDIDATE_REJECTED

    succeeding = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )
    second = heal(db_session, succeeding, incident, collector, brightdata_settings)

    assert second.attempt == 2
    assert second.recovered is True


@pytest.mark.parametrize("has_preview", [True, False])
def test_rejection_escalates_only_once_the_budget_is_exhausted(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
    has_preview: bool,
) -> None:
    """Three rejections, then a human -- and not one moment sooner.

    Parametrized over both ways a candidate can fail preflight, because
    an unvalidatable candidate and an invalid one are the same kind of
    failure and must burn the budget at the same rate.
    """
    gate = (
        awaiting_approval([broken_record(healthy_records, tam_score=60)])
        if has_preview
        else awaiting_approval_without_preview()
    )

    for attempt in range(1, MAX_AUTONOMOUS_REPAIR_ATTEMPTS):
        provider = ScriptedProvider(progress=[gate])
        result = heal(db_session, provider, incident, collector, brightdata_settings)

        assert result.attempt == attempt
        assert result.outcome is HealingOutcome.CANDIDATE_REJECTED
        assert provider.resume_bodies == [{"message": False, "auto_save": False}]
        db_session.refresh(incident)
        # Still repairable: the incident is handed back for another try.
        assert incident.status is IncidentStatus.DEGRADED
        assert incident.recommended_action is RecommendedAction.REQUEST_HEAL

    last = ScriptedProvider(progress=[gate])
    result = heal(db_session, last, incident, collector, brightdata_settings)

    assert result.attempt == MAX_AUTONOMOUS_REPAIR_ATTEMPTS
    # The final candidate is still rejected at the provider before the
    # incident is handed over -- the gate is never left hanging open.
    assert last.resume_bodies == [{"message": False, "auto_save": False}]
    assert result.outcome is HealingOutcome.ESCALATED
    assert result.candidate_approved is False
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.MANUAL_REVIEW
    assert incident.recommended_action is RecommendedAction.ESCALATE
    assert incident.repair_attempts == MAX_AUTONOMOUS_REPAIR_ATTEMPTS

    reasons = [
        event["event"]
        for event in incident.evidence["events"]
        if event["event"] in {"healing_failed", "escalated_to_manual_review"}
    ]
    # The rejection is recorded before the escalation, not instead of it.
    assert reasons[-2:] == ["healing_failed", "escalated_to_manual_review"]


def test_a_fourth_attempt_never_reaches_bright_data(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    bad_preview = [broken_record(healthy_records, tam_score=60)]
    for _ in range(MAX_AUTONOMOUS_REPAIR_ATTEMPTS):
        heal(
            db_session,
            ScriptedProvider(progress=[awaiting_approval(bad_preview)]),
            incident,
            collector,
            brightdata_settings,
        )

    fourth = ScriptedProvider(progress=[awaiting_approval(bad_preview)])
    result = heal(db_session, fourth, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.ESCALATED
    assert fourth.requests == []
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.MANUAL_REVIEW
    assert incident.recommended_action is RecommendedAction.ESCALATE
    assert incident.repair_attempts == MAX_AUTONOMOUS_REPAIR_ATTEMPTS


def test_provider_status_is_reported_without_leaking_the_token(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
    healthy_records: list[dict[str, Any]],
) -> None:
    provider = ScriptedProvider(
        progress=[awaiting_approval(good_records(3, healthy_records)), done()],
        dataset=good_records(10, healthy_records),
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.provider_status is HealingStatus.DONE
    db_session.refresh(incident)
    assert "test-token-do-not-log" not in json.dumps(incident.evidence)
    assert "test-token-do-not-log" not in json.dumps(incident.recovery_proof)


def test_a_trigger_failure_ends_the_attempt_without_a_candidate(
    db_session: Session,
    collector: Collector,
    incident: ReliabilityIncident,
    brightdata_settings: Settings,
) -> None:
    provider = ScriptedProvider(
        progress=[running()],
        heal_response=httpx.Response(503, json={"error": "unavailable"}),
    )

    result = heal(db_session, provider, incident, collector, brightdata_settings)

    assert result.outcome is HealingOutcome.PROVIDER_FAILED
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.DEGRADED
    assert not any(
        event["event"] == "repair_candidate_registered"
        for event in incident.evidence["events"]
    )
