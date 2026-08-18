import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Collector, ReliabilityIncident, Signal
from app.domain.enums import (
    FailureClassification,
    IncidentStatus,
    RecommendedAction,
    ReliabilityState,
    RunStatus,
)
from app.recallguard.errors import NonTerminalRunError
from app.recallguard.schemas import BaselineProfile, ReliabilityPolicy
from app.recallguard.service import (
    active_incident,
    collector_reliability_state,
    evaluate_collector_run,
)
from tests.recallguard.conftest import FakeClock, RunBuilder, invalid_record


def incident_count(db_session: Session) -> int:
    return db_session.execute(
        select(func.count()).select_from(ReliabilityIncident)
    ).scalar_one()


def evaluate(
    db_session: Session,
    run: object,
    *,
    baseline: BaselineProfile | None = None,
    policy: ReliabilityPolicy | None = None,
) -> object:
    kwargs = {"policy": policy} if policy is not None else {}
    return evaluate_collector_run(
        db_session, run=run, baseline=baseline, now=FakeClock(), **kwargs
    )


# --- healthy ---------------------------------------------------------------


def test_healthy_run_passes_and_opens_no_incident(
    db_session: Session,
    collector: Collector,
    runs: RunBuilder,
    healthy_baseline: BaselineProfile,
) -> None:
    run = runs.succeeded(record_count=healthy_baseline.record_count)

    evaluation = evaluate(db_session, run, baseline=healthy_baseline)

    assert evaluation.passed is True
    assert evaluation.state is ReliabilityState.HEALTHY
    assert evaluation.incident_id is None
    assert evaluation.classification is None
    assert incident_count(db_session) == 0
    assert (
        collector_reliability_state(db_session, collector_id=collector.id)
        is ReliabilityState.HEALTHY
    )


def test_more_records_than_baseline_is_healthy(
    db_session: Session, runs: RunBuilder, healthy_baseline: BaselineProfile
) -> None:
    run = runs.succeeded(record_count=healthy_baseline.record_count + 40)

    evaluation = evaluate(db_session, run, baseline=healthy_baseline)

    assert evaluation.passed is True
    assert incident_count(db_session) == 0


def test_new_industry_alone_is_healthy(
    db_session: Session,
    runs: RunBuilder,
    healthy_baseline: BaselineProfile,
) -> None:
    # A shifting industry vocabulary is normal source behavior: the
    # baseline records how many industries were seen, and RecallGuard
    # never penalizes a different set.
    shifted = BaselineProfile(
        label="fix_my_itch_healthy_v1",
        record_count=healthy_baseline.record_count,
        industry_count=healthy_baseline.industry_count + 3,
    )
    run = runs.succeeded(record_count=healthy_baseline.record_count)

    evaluation = evaluate(db_session, run, baseline=shifted)

    assert evaluation.passed is True
    assert incident_count(db_session) == 0


def test_run_without_a_baseline_skips_completeness(
    db_session: Session, runs: RunBuilder
) -> None:
    run = runs.succeeded(record_count=0)

    evaluation = evaluate(db_session, run, baseline=None)

    assert evaluation.passed is True
    completeness = next(c for c in evaluation.checks if c.name == "completeness")
    assert "without a non-zero baseline" in (completeness.detail or "")


def test_non_terminal_run_cannot_be_evaluated(
    db_session: Session, runs: RunBuilder
) -> None:
    run = runs.succeeded()
    run.status = RunStatus.RUNNING
    db_session.commit()

    with pytest.raises(NonTerminalRunError):
        evaluate(db_session, run)


# --- provider outage -------------------------------------------------------


@pytest.mark.parametrize("stage", ["trigger", "collection", "timeout"])
def test_provider_failure_is_an_outage_to_retry(
    db_session: Session, runs: RunBuilder, stage: str
) -> None:
    # A provider outage must never be answered by healing the scraper:
    # nothing about the extraction logic is implicated.
    run = runs.failed(stage)

    evaluation = evaluate(db_session, run)

    assert evaluation.passed is False
    assert evaluation.classification is FailureClassification.OUTAGE
    assert evaluation.recommended_action is RecommendedAction.RETRY
    assert evaluation.state is ReliabilityState.DEGRADED
    incident = active_incident(db_session, collector_id=run.collector_id)
    assert incident is not None
    assert incident.status is IncidentStatus.DEGRADED


# --- extraction drift ------------------------------------------------------


def test_malformed_payload_is_extraction_drift(
    db_session: Session, runs: RunBuilder
) -> None:
    run = runs.failed("payload")

    evaluation = evaluate(db_session, run)

    assert evaluation.classification is FailureClassification.EXTRACTION_DRIFT
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    transport = next(c for c in evaluation.checks if c.name == "transport_payload")
    assert transport.passed is False


def test_historical_tam_score_60_is_drift_and_stays_evidence(
    db_session: Session, runs: RunBuilder
) -> None:
    # The scraper populated every field and still produced a wrong value.
    # RecallGuard must surface it, never rescale it into a plausible 6.
    run = runs.source_validation_failed(
        invalid_records=[invalid_record(index=1, tam_score=60)]
    )

    evaluation = evaluate(db_session, run)

    assert evaluation.classification is FailureClassification.EXTRACTION_DRIFT
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None
    sample = incident.evidence["occurrences"][0]["sample_violations"][0]
    assert sample["raw"]["tam_score"] == 60
    assert sample["index"] == 1


@pytest.mark.parametrize(
    ("reason", "overrides"),
    [
        ("invalid_record", {"confidence_score": 4}),
        ("missing_required_field", {"problem": ""}),
        ("invalid_source", {"source": "arxiv"}),
        ("invalid_source_url", {"source_url": "https://evil.example.com/"}),
        ("invalid_score", {"severity_score": 42}),
    ],
)
def test_source_contract_violations_are_extraction_drift(
    db_session: Session, runs: RunBuilder, reason: str, overrides: dict
) -> None:
    run = runs.source_validation_failed(
        invalid_records=[invalid_record(reason=reason, **overrides)]
    )

    evaluation = evaluate(db_session, run)

    assert evaluation.classification is FailureClassification.EXTRACTION_DRIFT
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    contract = next(c for c in evaluation.checks if c.name == "source_contract")
    assert contract.passed is False


# --- completeness ----------------------------------------------------------


def test_zero_records_against_a_baseline_degrades_a_successful_run(
    db_session: Session, runs: RunBuilder, healthy_baseline: BaselineProfile
) -> None:
    run = runs.succeeded(record_count=0)

    evaluation = evaluate(db_session, run, baseline=healthy_baseline)

    # Execution genuinely succeeded and is left untouched...
    assert run.status is RunStatus.SUCCEEDED
    # ...but reliability did not.
    assert evaluation.passed is False
    assert evaluation.state is ReliabilityState.DEGRADED
    assert evaluation.classification is FailureClassification.EXTRACTION_DRIFT
    assert evaluation.recommended_action is RecommendedAction.REQUEST_HEAL
    completeness = next(c for c in evaluation.checks if c.name == "completeness")
    assert completeness.observed == "0 records"


def test_zero_records_is_not_classified_source_absence(
    db_session: Session, runs: RunBuilder, healthy_baseline: BaselineProfile
) -> None:
    # An empty dataset looks identical whether the scraper broke or the
    # data is genuinely gone. Assuming the latter would quietly accept a
    # broken scraper, so SOURCE_ABSENCE is never inferred.
    evaluation = evaluate(
        db_session, runs.succeeded(record_count=0), baseline=healthy_baseline
    )

    assert evaluation.classification is not FailureClassification.SOURCE_ABSENCE


@pytest.mark.parametrize(
    ("observed", "passes"),
    [
        (100, True),  # no drop
        (50, True),  # exactly at the 50% limit -- allowed
        (49, False),  # past the limit
        (1, False),
    ],
)
def test_relative_drop_policy_boundary(
    db_session: Session, runs: RunBuilder, observed: int, passes: bool
) -> None:
    baseline = BaselineProfile(label="synthetic", record_count=100)
    policy = ReliabilityPolicy(max_relative_record_drop=0.5)

    evaluation = evaluate(
        db_session,
        runs.succeeded(record_count=observed),
        baseline=baseline,
        policy=policy,
    )

    assert evaluation.passed is passes


def test_relative_drop_policy_can_be_disabled(
    db_session: Session, runs: RunBuilder
) -> None:
    baseline = BaselineProfile(label="synthetic", record_count=100)

    evaluation = evaluate(
        db_session,
        runs.succeeded(record_count=1),
        baseline=baseline,
        policy=ReliabilityPolicy(max_relative_record_drop=None),
    )

    assert evaluation.passed is True


def test_zero_records_fails_even_with_the_drop_policy_disabled(
    db_session: Session, runs: RunBuilder
) -> None:
    baseline = BaselineProfile(label="synthetic", record_count=100)

    evaluation = evaluate(
        db_session,
        runs.succeeded(record_count=0),
        baseline=baseline,
        policy=ReliabilityPolicy(max_relative_record_drop=None),
    )

    assert evaluation.passed is False


# --- internal failures -----------------------------------------------------


def test_ingestion_failure_is_unknown_and_investigated(
    db_session: Session, runs: RunBuilder
) -> None:
    # GapRadar's own database failing is not the scraper's fault, and
    # must never trigger a heal request.
    run = runs.failed("ingestion", extra={"rejected_records": [{"index": 0}]})

    evaluation = evaluate(db_session, run)

    assert evaluation.classification is FailureClassification.UNKNOWN
    assert evaluation.recommended_action is RecommendedAction.INVESTIGATE


def test_unrecognized_stage_is_unknown(db_session: Session, runs: RunBuilder) -> None:
    run = runs.failed("something_new")

    evaluation = evaluate(db_session, run)

    assert evaluation.classification is FailureClassification.UNKNOWN
    assert evaluation.recommended_action is RecommendedAction.INVESTIGATE


# --- incident dedup --------------------------------------------------------


def test_repeated_failures_update_one_incident(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    first = evaluate(db_session, runs.failed("collection"))
    second = evaluate(db_session, runs.failed("collection"))
    third = evaluate(db_session, runs.failed("collection"))

    assert first.incident_id == second.incident_id == third.incident_id
    assert incident_count(db_session) == 1
    incident = db_session.get(ReliabilityIncident, first.incident_id)
    assert incident is not None
    assert len(incident.evidence["occurrences"]) == 3


def test_a_later_different_failure_updates_the_same_active_incident(
    db_session: Session, runs: RunBuilder
) -> None:
    outage = evaluate(db_session, runs.failed("timeout"))
    drift = evaluate(db_session, runs.failed("payload"))

    assert drift.incident_id == outage.incident_id
    assert incident_count(db_session) == 1
    # The newest diagnosis wins while the incident is still degraded.
    assert drift.classification is FailureClassification.EXTRACTION_DRIFT
    assert drift.recommended_action is RecommendedAction.REQUEST_HEAL


def test_a_passing_run_does_not_close_an_active_incident(
    db_session: Session, collector: Collector, runs: RunBuilder
) -> None:
    # Only an explicit verification closes an incident. A single good run
    # is not proof that a repair worked.
    degraded = evaluate(db_session, runs.failed("payload"))

    evaluation = evaluate(db_session, runs.succeeded(record_count=133))

    assert evaluation.passed is True
    assert evaluation.state is ReliabilityState.DEGRADED
    assert evaluation.incident_id == degraded.incident_id
    incident = db_session.get(ReliabilityIncident, degraded.incident_id)
    assert incident is not None
    assert incident.status is IncidentStatus.DEGRADED
    assert incident.recovered_at is None


def test_incidents_are_scoped_per_collector(
    db_session: Session,
    collector: Collector,
    other_collector: Collector,
    runs: RunBuilder,
) -> None:
    evaluate(db_session, runs.failed("payload"))
    other_runs = RunBuilder(db_session, other_collector)

    evaluate(db_session, other_runs.failed("payload"))

    assert incident_count(db_session) == 2
    assert (
        collector_reliability_state(db_session, collector_id=other_collector.id)
        is ReliabilityState.DEGRADED
    )


# --- non-interference ------------------------------------------------------


def test_evaluation_modifies_no_source_data(
    db_session: Session, runs: RunBuilder, healthy_baseline: BaselineProfile
) -> None:
    run = runs.source_validation_failed(invalid_records=[invalid_record(tam_score=60)])
    before = dict(run.raw_metadata)

    evaluate(db_session, run, baseline=healthy_baseline)
    db_session.refresh(run)

    assert run.raw_metadata == before
    assert run.status is RunStatus.FAILED
    assert run.record_count == 0
    assert (
        db_session.execute(select(func.count()).select_from(Signal)).scalar_one() == 0
    )
