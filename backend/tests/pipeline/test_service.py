"""What the pipeline may and may not do on its way from raw data to trust.

The boundary these tests defend: the pipeline decides *whether* a repair
may be attempted, and RecallGuard decides everything about what the
result means. Bright Data DONE is not RecallGuard HEALTHY, an approved
candidate is not a recovered incident, and no run these tests drive ever
reaches the real provider.
"""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.collection.errors import (
    CollectionIngestionError,
    CollectionTriggerError,
    SourceContractValidationError,
)
from app.collection.schemas import CollectionRunResult, PollingPolicy
from app.config import Settings
from app.db.models import Collector, CollectorRun, ReliabilityIncident
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
    RunStatus,
)
from app.pipeline.schemas import PipelineOutcome
from app.pipeline.service import baseline_from_history, run_pipeline
from app.recallguard.healing import (
    HealingAttemptResult,
    HealingOutcome,
    SelfHealingPolicy,
)
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    MAX_AUTONOMOUS_REPAIR_ATTEMPTS,
    evaluate_collector_run,
)
from tests.integrations.brightdata.conftest import make_client
from tests.pipeline.conftest import RepairableProvider
from tests.recallguard.conftest import FakeClock, RunBuilder, invalid_record
from tests.recallguard.healing_fakes import (
    HealClock,
    ScriptedProvider,
    awaiting_approval,
    done,
    provider_running,
)

BASELINE = BaselineProfile(label="fix_my_itch_healthy_v1", record_count=5)
FAST_HEALING = SelfHealingPolicy(interval_seconds=1.0, timeout_seconds=60.0)
FAST_COLLECTION = PollingPolicy(interval_seconds=1.0, timeout_seconds=60.0)


class SpyHealer:
    """Stands in for execute_healing_attempt and records every call.

    Used only to prove the pipeline delegates rather than repairs: the
    real orchestration is exercised end to end further down this file.
    """

    def __init__(self, result: HealingAttemptResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result

    def __call__(
        self, session: Session, client: Any, **kwargs: Any
    ) -> HealingAttemptResult:
        self.calls.append(kwargs)
        incident = kwargs["incident"]
        return self.result or HealingAttemptResult(
            incident_id=incident.id,
            attempt=incident.repair_attempts,
            outcome=HealingOutcome.PROVIDER_FAILED,
            detail="spy",
        )


def collects(run: CollectorRun) -> Any:
    """A collection stand-in that yields an already-persisted run."""

    def _collect(session: Session, client: Any, **kwargs: Any) -> CollectionRunResult:
        return CollectionRunResult(
            collector_run_id=run.id,
            external_run_id=run.external_run_id,
            status=run.status,
            fetched_record_count=run.record_count,
            valid_record_count=run.record_count,
            accepted=run.record_count,
        )

    return _collect


def fails_with(exc: Exception) -> Any:
    def _collect(session: Session, client: Any, **kwargs: Any) -> CollectionRunResult:
        raise exc

    return _collect


# -- the healthy path -------------------------------------------------------


def test_a_healthy_collection_is_trusted_and_never_asks_for_a_repair(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    run = runs.succeeded(record_count=5)
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(run),
        heal=healer,
        now=FakeClock(),
    )

    assert result.outcome is PipelineOutcome.HEALTHY
    assert result.reliability_state is ReliabilityState.HEALTHY
    assert result.trusted is True
    assert result.trusted_collector_run_id == run.id
    assert result.incident_id is None
    assert healer.calls == []


# -- delegation, and refusal to delegate ------------------------------------


def test_extraction_drift_delegates_to_the_existing_healing_orchestration(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    run = runs.source_validation_failed(
        invalid_records=[invalid_record(tam_score=60)], fetched=5
    )
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(run),
        heal=healer,
        now=FakeClock(),
    )

    assert len(healer.calls) == 1
    delegated = healer.calls[0]
    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    assert delegated["incident"].id == incident.id
    assert delegated["collector"].id == collector.id
    assert incident.classification is FailureClassification.EXTRACTION_DRIFT
    assert incident.recommended_action is RecommendedAction.REQUEST_HEAL
    # The pipeline itself performed no repair bookkeeping: the attempt
    # count is whatever the healing orchestration left it at.
    assert incident.repair_attempts == 0
    assert result.healing_skipped_reason is None


def test_a_provider_outage_never_reaches_the_healer(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    run = runs.failed("timeout")
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(run),
        heal=healer,
        now=FakeClock(),
    )

    assert healer.calls == []
    assert result.outcome is PipelineOutcome.DEGRADED
    assert result.trusted is False
    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    assert incident.classification is FailureClassification.OUTAGE
    assert incident.recommended_action is RecommendedAction.RETRY
    assert "retry" in (result.healing_skipped_reason or "")


def test_an_unknown_internal_failure_never_reaches_the_healer(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    """GapRadar's own ingestion failing is never the scraper's fault."""
    run = runs.failed("ingestion")
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(run),
        heal=healer,
        now=FakeClock(),
    )

    assert healer.calls == []
    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    assert incident.classification is FailureClassification.UNKNOWN
    assert incident.recommended_action is RecommendedAction.INVESTIGATE
    assert result.trusted is False


def test_an_escalated_incident_is_left_with_the_human(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    clock = FakeClock()
    first = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=5
        ),
        baseline=BASELINE,
        now=clock,
    )
    incident = db_session.get(ReliabilityIncident, first.incident_id)
    assert incident is not None
    incident.status = IncidentStatus.MANUAL_REVIEW
    incident.recommended_action = RecommendedAction.ESCALATE
    db_session.commit()

    healer = SpyHealer()
    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(
            runs.source_validation_failed(
                invalid_records=[invalid_record(tam_score=60)], fetched=5
            )
        ),
        heal=healer,
        now=clock,
    )

    assert healer.calls == []
    assert result.outcome is PipelineOutcome.MANUAL_REVIEW
    assert result.reliability_state is ReliabilityState.MANUAL_REVIEW
    assert result.trusted is False
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.MANUAL_REVIEW


def test_healing_can_be_switched_off_without_changing_the_verdict(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        allow_healing=False,
        collect=collects(
            runs.source_validation_failed(
                invalid_records=[invalid_record(tam_score=60)], fetched=5
            )
        ),
        heal=healer,
        now=FakeClock(),
    )

    assert healer.calls == []
    assert result.outcome is PipelineOutcome.DEGRADED
    assert result.healing_skipped_reason == "healing disabled for this pipeline run"


# -- trust ------------------------------------------------------------------


def test_bad_data_never_becomes_trusted_downstream_output(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    """A drifted run, and a repair that did not work, are both untrusted."""
    drifted = runs.source_validation_failed(
        invalid_records=[invalid_record(tam_score=60)], fetched=5
    )
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(drifted),
        heal=healer,
        now=FakeClock(),
    )

    assert result.trusted is False
    assert result.trusted_collector_run_id is None
    assert result.reliability_state is not ReliabilityState.HEALTHY
    assert result.outcome is PipelineOutcome.HEALING_FAILED


def test_an_approved_candidate_alone_is_never_trusted(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    """APPROVED != RECOVERED, even when the provider is happy."""
    drifted = runs.source_validation_failed(
        invalid_records=[invalid_record(tam_score=60)], fetched=5
    )
    approved_but_unverified = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        baseline=BASELINE,
        collect=collects(drifted),
        heal=approved_but_unverified,
        now=FakeClock(),
    )

    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    assert incident.status is not IncidentStatus.RECOVERED
    assert incident.recovery_proof is None
    assert result.trusted is False


# -- unevaluable collection -------------------------------------------------


def test_a_trigger_failure_invents_neither_a_run_nor_an_incident(
    db_session: Session, collector: Collector
) -> None:
    healer = SpyHealer()

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        collect=fails_with(CollectionTriggerError("bright data said no")),
        heal=healer,
        now=FakeClock(),
    )

    assert result.outcome is PipelineOutcome.COLLECTION_UNEVALUABLE
    assert result.trusted is False
    assert result.collector_run_id is None
    assert result.incident_id is None
    assert result.collection_failure is not None
    assert result.collection_failure.stage == "trigger"
    assert healer.calls == []
    assert db_session.query(ReliabilityIncident).count() == 0


def test_a_failed_collection_is_still_evaluated_not_discarded(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    run = runs.failed("ingestion")
    failure = CollectionIngestionError(
        "persisting failed", collector_run_id=run.id, rejected=[]
    )

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        collect=fails_with(failure),
        heal=SpyHealer(),
        now=FakeClock(),
    )

    assert result.collection_failure is not None
    assert result.collection_failure.stage == "ingestion"
    assert result.evaluation is not None
    assert result.evaluation.passed is False
    assert result.incident_id is not None


def test_an_unexpected_exception_is_never_swallowed(
    db_session: Session, collector: Collector
) -> None:
    with pytest.raises(RuntimeError, match="disk on fire"):
        run_pipeline(
            db_session,
            client=None,  # type: ignore[arg-type]
            collector=collector,
            collect=fails_with(RuntimeError("disk on fire")),
            heal=SpyHealer(),
            now=FakeClock(),
        )


# -- the whole cycle, against a mocked provider -----------------------------


def test_only_a_fresh_independently_verified_run_produces_recovered(
    db_session: Session,
    collector: Collector,
    brightdata_settings: Settings,
    good_records: list[dict[str, Any]],
    drifted_records: list[dict[str, Any]],
) -> None:
    """The narrative, end to end, through the real modules.

    Real collection orchestrator, real RecallGuard, real healing
    orchestration, real BrightDataClient -- only the HTTP transport is a
    fake. The first collection carries the deliberate TAM fault; recovery
    arrives only from the second, independent collection that ran after
    the repair was approved.
    """
    provider = RepairableProvider(
        broken=drifted_records,
        healed=good_records,
        progress=[awaiting_approval(good_records), done()],
    )
    clock = HealClock()

    with make_client(brightdata_settings, provider) as client:
        result = run_pipeline(
            db_session,
            client,
            collector=collector,
            healing_policy=FAST_HEALING,
            collection_polling=FAST_COLLECTION,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert result.outcome is PipelineOutcome.RECOVERED
    assert result.reliability_state is ReliabilityState.HEALTHY
    assert result.trusted is True

    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    assert incident.status is IncidentStatus.RECOVERED
    assert incident.recovery_proof is not None
    assert incident.repair_attempts == 1

    # The proof names a run that is not the one that detected the fault,
    # and that run is the only one whose data is trusted.
    assert incident.verification_run_id != incident.detection_run_id
    assert result.trusted_collector_run_id == incident.verification_run_id
    verification = db_session.get(CollectorRun, incident.verification_run_id)
    assert verification is not None
    assert verification.status is RunStatus.SUCCEEDED


def test_the_repair_budget_is_not_bypassed_by_running_the_pipeline_again(
    db_session: Session,
    collector: Collector,
    runs: RunBuilder,
    brightdata_settings: Settings,
    drifted_records: list[dict[str, Any]],
    good_records: list[dict[str, Any]],
) -> None:
    """A fourth autonomous repair is refused, and the healer is not called."""
    clock = FakeClock()
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=5
        ),
        baseline=BASELINE,
        now=clock,
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None
    incident.repair_attempts = MAX_AUTONOMOUS_REPAIR_ATTEMPTS
    db_session.commit()

    provider = RepairableProvider(
        broken=drifted_records, healed=good_records, progress=[done()]
    )
    heal_clock = HealClock()
    with make_client(brightdata_settings, provider) as client:
        result = run_pipeline(
            db_session,
            client,
            collector=collector,
            healing_policy=FAST_HEALING,
            collection_polling=FAST_COLLECTION,
            now=heal_clock.now,
            sleep=heal_clock.sleep,
        )

    assert provider.heal_requests == []
    assert result.outcome is PipelineOutcome.MANUAL_REVIEW
    assert result.trusted is False
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.MANUAL_REVIEW
    assert incident.repair_attempts == MAX_AUTONOMOUS_REPAIR_ATTEMPTS


def test_a_source_contract_violation_is_diagnosed_as_drift_end_to_end(
    db_session: Session,
    collector: Collector,
    brightdata_settings: Settings,
    drifted_records: list[dict[str, Any]],
) -> None:
    """The provider never repairs itself, so the incident stays open."""
    provider = ScriptedProvider(
        progress=[awaiting_approval(drifted_records)], dataset=drifted_records
    )
    clock = HealClock()

    with make_client(brightdata_settings, provider) as client:
        result = run_pipeline(
            db_session,
            client,
            collector=collector,
            healing_policy=FAST_HEALING,
            collection_polling=FAST_COLLECTION,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert result.outcome is PipelineOutcome.HEALING_FAILED
    assert result.trusted is False
    assert result.healing is not None
    # The candidate's own preview still violates the contract, so it was
    # rejected rather than approved.
    assert result.healing.outcome is HealingOutcome.CANDIDATE_REJECTED
    assert result.healing.candidate_approved is False
    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovery_proof is None


# -- baseline ---------------------------------------------------------------


def test_baseline_comes_from_the_largest_run_the_collector_really_delivered(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    runs.succeeded(record_count=133)
    runs.succeeded(record_count=4)
    runs.failed("timeout")

    baseline = baseline_from_history(db_session, collector_id=collector.id)

    assert baseline is not None
    assert baseline.record_count == 133


def test_no_successful_history_means_no_baseline_rather_than_a_guess(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    runs.failed("timeout")

    assert baseline_from_history(db_session, collector_id=collector.id) is None


def test_the_run_being_judged_is_excluded_from_its_own_baseline(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    previous = runs.succeeded(record_count=133)
    current = runs.succeeded(record_count=3)

    baseline = baseline_from_history(
        db_session, collector_id=collector.id, exclude_run_id=current.id
    )

    assert baseline is not None
    assert baseline.record_count == previous.record_count


def test_source_contract_violation_error_is_reported_verbatim(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    """The pipeline reports the stage; it never rescales the bad value."""
    run = runs.source_validation_failed(
        invalid_records=[invalid_record(tam_score=60)], fetched=5
    )
    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        collect=collects(run),
        heal=SpyHealer(),
        now=FakeClock(),
    )

    incident = db_session.get(ReliabilityIncident, result.incident_id)
    assert incident is not None
    violations = incident.evidence["occurrences"][0]["sample_violations"]
    assert violations[0]["raw"]["tam_score"] == 60


def test_source_validation_error_from_the_collector_keeps_its_stage(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    run = runs.source_validation_failed(
        invalid_records=[invalid_record(tam_score=60)], fetched=5
    )
    report_error = SourceContractValidationError.__new__(SourceContractValidationError)
    Exception.__init__(report_error, "1 record violated the source contract")
    report_error.collector_run_id = run.id

    result = run_pipeline(
        db_session,
        client=None,  # type: ignore[arg-type]
        collector=collector,
        collect=fails_with(report_error),
        heal=SpyHealer(),
        now=FakeClock(),
    )

    assert result.collection_failure is not None
    assert result.collection_failure.stage == "source_validation"


# -- resuming a repair the provider is already running ----------------------


def in_flight_incident(
    db_session: Session, runs: RunBuilder, *, status: IncidentStatus
) -> ReliabilityIncident:
    """An incident whose attempt 1 is still alive at the provider.

    Reproduces the production state: DEGRADED after a local timeout, or
    HEALING after the process was killed mid-attempt. Either way
    repair_attempts is 1 and Bright Data is still working.
    """
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(tam_score=60)], fetched=5
        ),
        baseline=BASELINE,
        now=FakeClock(),
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None
    incident.repair_attempts = 1
    incident.status = status
    db_session.commit()
    return incident


@pytest.mark.parametrize("status", [IncidentStatus.DEGRADED, IncidentStatus.HEALING])
def test_the_pipeline_resumes_a_live_repair_instead_of_starting_another(
    db_session: Session,
    collector: Collector,
    runs: RunBuilder,
    brightdata_settings: Settings,
    drifted_records: list[dict[str, Any]],
    status: IncidentStatus,
) -> None:
    """The daily 08:00 case: yesterday's repair is still running.

    A DEGRADED incident is what a local timeout leaves behind; a HEALING
    one is what a killed process leaves behind. Neither may trigger a
    second repair.
    """
    incident = in_flight_incident(db_session, runs, status=status)
    provider = ScriptedProvider(
        progress=[provider_running()],
        dataset=drifted_records,
        repair_in_flight=True,
    )
    clock = HealClock()

    with make_client(brightdata_settings, provider) as client:
        result = run_pipeline(
            db_session,
            client,
            collector=collector,
            baseline=BASELINE,
            healing_policy=SelfHealingPolicy(interval_seconds=2.0, timeout_seconds=6.0),
            collection_polling=FAST_COLLECTION,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert provider.heal_requests == []
    assert result.outcome is PipelineOutcome.HEALING_IN_PROGRESS
    assert result.trusted is False
    assert result.healing is not None
    assert result.healing.outcome is HealingOutcome.LOCAL_TIMEOUT
    assert result.healing.attempt == 1
    db_session.refresh(incident)
    assert incident.repair_attempts == 1


def test_the_pipeline_finishes_a_resumed_repair_it_did_not_start(
    db_session: Session,
    collector: Collector,
    runs: RunBuilder,
    brightdata_settings: Settings,
    good_records: list[dict[str, Any]],
    drifted_records: list[dict[str, Any]],
) -> None:
    """The repair reaches its gate on a later invocation and is proven."""
    incident = in_flight_incident(db_session, runs, status=IncidentStatus.DEGRADED)
    provider = RepairableProvider(
        broken=drifted_records,
        healed=good_records,
        progress=[awaiting_approval(good_records), done()],
    )
    provider.repair_in_flight = True
    clock = HealClock()

    with make_client(brightdata_settings, provider) as client:
        result = run_pipeline(
            db_session,
            client,
            collector=collector,
            healing_policy=FAST_HEALING,
            collection_polling=FAST_COLLECTION,
            now=clock.now,
            sleep=clock.sleep,
        )

    assert provider.heal_requests == []
    assert result.outcome is PipelineOutcome.RECOVERED
    assert result.trusted is True
    db_session.refresh(incident)
    assert incident.status is IncidentStatus.RECOVERED
    # The repair that ran was attempt 1, and it stayed attempt 1.
    assert incident.repair_attempts == 1
    assert incident.recovery_proof is not None
    assert incident.recovery_proof["repair_attempt"] == 1
