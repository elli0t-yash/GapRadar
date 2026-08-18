"""Deterministic detection: what does one collector run tell us?

Pure functions over evidence the collection orchestrator already
persisted on the run. No database writes, no provider calls, no LLM, and
above all no repair of source data -- a tam_score of 60 stays 60 here and
in the incident evidence, because the wrong value is the finding.
"""

from typing import Any

from app.db.models import CollectorRun
from app.domain.enums import FailureClassification, RecommendedAction, RunStatus
from app.recallguard.schemas import BaselineProfile, CheckResult, ReliabilityPolicy

# Orchestration stage -> what that failure means and what to do about it.
# Written out rather than inferred so the reasoning is auditable, and so
# a provider outage can never be mistaken for scraper drift.
_STAGE_DIAGNOSIS: dict[str, tuple[FailureClassification, RecommendedAction]] = {
    # The provider could not run the collection at all.
    "trigger": (FailureClassification.OUTAGE, RecommendedAction.RETRY),
    "collection": (FailureClassification.OUTAGE, RecommendedAction.RETRY),
    "timeout": (FailureClassification.OUTAGE, RecommendedAction.RETRY),
    # The collection ran and returned something unusable or wrong.
    "payload": (FailureClassification.EXTRACTION_DRIFT, RecommendedAction.REQUEST_HEAL),
    "source_validation": (
        FailureClassification.EXTRACTION_DRIFT,
        RecommendedAction.REQUEST_HEAL,
    ),
    # GapRadar's own persistence failed. Never the scraper's fault, so
    # never a reason to ask Bright Data to repair anything.
    "ingestion": (FailureClassification.UNKNOWN, RecommendedAction.INVESTIGATE),
}

_UNDIAGNOSED = (FailureClassification.UNKNOWN, RecommendedAction.INVESTIGATE)

# How many offending records are copied into an incident. Bounded on
# purpose: the run's own metadata already holds the full list, and an
# incident is a summary, not a second copy of the payload.
MAX_SAMPLE_VIOLATIONS = 5


def orchestration(run: CollectorRun) -> dict[str, Any]:
    """The orchestrator's own account of the run, or {} if absent."""
    metadata = run.raw_metadata or {}
    section = metadata.get("orchestration")
    return section if isinstance(section, dict) else {}


def observed_record_count(run: CollectorRun) -> int:
    counted = orchestration(run).get("fetched_record_count")
    if isinstance(counted, int):
        return counted
    return run.record_count


def diagnose_stage(
    stage: str | None,
) -> tuple[FailureClassification, RecommendedAction]:
    return _STAGE_DIAGNOSIS.get(stage or "", _UNDIAGNOSED)


def check_execution(run: CollectorRun) -> CheckResult:
    return CheckResult(
        name="execution",
        passed=run.status is RunStatus.SUCCEEDED,
        expected="collector run reaches SUCCEEDED",
        observed=run.status.value,
        detail=orchestration(run).get("message")
        if run.status is RunStatus.FAILED
        else None,
    )


def check_source_contract(run: CollectorRun) -> CheckResult:
    """Whether every record satisfied the Fix My Itch source contract.

    A run can populate every field of every record and still be wrong:
    this check is about values, not presence.
    """
    section = orchestration(run)
    invalid = section.get("invalid_record_count")
    stage = section.get("stage")

    if stage == "source_validation":
        return CheckResult(
            name="source_contract",
            passed=False,
            expected="every record satisfies the source contract",
            observed=f"{invalid if isinstance(invalid, int) else 'some'} record(s) violated it",
            detail=section.get("message"),
        )
    return CheckResult(
        name="source_contract",
        passed=True,
        expected="every record satisfies the source contract",
        observed="0 violations",
    )


def check_transport(run: CollectorRun) -> CheckResult:
    stage = orchestration(run).get("stage")
    failed = stage == "payload"
    return CheckResult(
        name="transport_payload",
        passed=not failed,
        expected="dataset is a JSON array of record objects",
        observed="malformed dataset payload" if failed else "well-formed dataset",
        detail=orchestration(run).get("message") if failed else None,
    )


def check_ingestion(run: CollectorRun) -> CheckResult:
    stage = orchestration(run).get("stage")
    failed = stage == "ingestion"
    return CheckResult(
        name="ingestion",
        passed=not failed,
        expected="validated records persist",
        observed="ingestion failed" if failed else "ingestion completed",
        detail=orchestration(run).get("message") if failed else None,
    )


def check_completeness(
    run: CollectorRun,
    *,
    baseline: BaselineProfile | None,
    policy: ReliabilityPolicy,
) -> CheckResult:
    """Completeness against an observed baseline -- never against a fixed
    expected size.

    Growth passes. New or missing industries pass. Only the two policy
    conditions fail: nothing at all where there used to be something, and
    (if configured) a drop larger than the allowed fraction.
    """
    observed = observed_record_count(run)

    if baseline is None or baseline.record_count == 0:
        return CheckResult(
            name="completeness",
            passed=True,
            expected="no baseline to compare against",
            observed=f"{observed} records",
            detail="completeness not evaluated without a non-zero baseline",
        )

    if observed == 0:
        return CheckResult(
            name="completeness",
            passed=False,
            expected=f"records present (baseline {baseline.label}: {baseline.record_count})",
            observed="0 records",
            detail="collection succeeded but returned nothing",
        )

    drop = (baseline.record_count - observed) / baseline.record_count
    limit = policy.max_relative_record_drop
    if limit is not None and drop > limit:
        return CheckResult(
            name="completeness",
            passed=False,
            expected=(
                f"at most a {limit:.0%} drop from baseline "
                f"{baseline.label} ({baseline.record_count} records)"
            ),
            observed=f"{observed} records ({drop:.0%} drop)",
            detail="operational completeness policy, not a source contract",
        )

    return CheckResult(
        name="completeness",
        passed=True,
        expected=f"baseline {baseline.label}: {baseline.record_count} records",
        observed=f"{observed} records",
    )


def evaluate_run_checks(
    run: CollectorRun,
    *,
    baseline: BaselineProfile | None,
    policy: ReliabilityPolicy,
) -> list[CheckResult]:
    """Every check, in a stable order, for one terminal run."""
    checks = [
        check_execution(run),
        check_transport(run),
        check_source_contract(run),
        check_ingestion(run),
    ]
    # Completeness is only meaningful once the collection actually
    # produced a dataset; a failed run's record count says nothing.
    if run.status is RunStatus.SUCCEEDED:
        checks.append(check_completeness(run, baseline=baseline, policy=policy))
    return checks


def diagnose(
    run: CollectorRun, checks: list[CheckResult]
) -> tuple[FailureClassification, RecommendedAction]:
    """Classify a failing run.

    A failed execution is diagnosed from the orchestration stage. A run
    that executed successfully but failed a reliability check is
    extraction drift: the collector reported success while returning
    materially less than the source is known to hold, which is what a
    broken selector looks like from the outside. It is deliberately NOT
    classified SOURCE_ABSENCE -- "the scraper broke" and "the data is
    gone" are indistinguishable from an empty dataset, and assuming the
    latter would quietly accept a broken scraper.
    """
    if run.status is not RunStatus.SUCCEEDED:
        return diagnose_stage(orchestration(run).get("stage"))
    if any(check.name == "completeness" and not check.passed for check in checks):
        return (
            FailureClassification.EXTRACTION_DRIFT,
            RecommendedAction.REQUEST_HEAL,
        )
    return _UNDIAGNOSED


def sample_violations(run: CollectorRun) -> list[dict[str, Any]]:
    """A bounded sample of the offending records, copied verbatim.

    Verbatim matters: a tam_score of 60 must stay 60 in the evidence.
    RecallGuard never rewrites source data, not even to make it look
    plausible.
    """
    invalid = orchestration(run).get("invalid_records")
    if not isinstance(invalid, list):
        return []
    return invalid[:MAX_SAMPLE_VIOLATIONS]
