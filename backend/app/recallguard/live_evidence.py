"""Read-only evidence from the isolated real Bright Data healing experiment.

The deterministic dashboard replay lives in ``app.recallguard.demo``. This
module deliberately does not import it: fixture evidence and provider evidence
must remain different products with different provenance.

No provider call and no database write occurs here. The view is reconstructed
only from the collector, runs, signals, and incident history already persisted
by the production collection and RecallGuard paths.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident, Signal
from app.schemas.reliability import (
    LiveBrightDataEvidenceRead,
    LiveEvidenceAutomationStage,
    LiveEvidenceCollector,
    LiveEvidenceDetection,
    LiveEvidenceFailedCheck,
    LiveEvidenceInvalidRecord,
    LiveEvidenceRepairAttempt,
    LiveEvidenceRun,
    LiveEvidenceVerification,
    LiveEvidenceVerificationSample,
)

LIVE_HEALING_PROVIDER = "brightdata"
LIVE_HEALING_EXTERNAL_COLLECTOR_ID = "c_msya3ha629w2q9c62m"
MAX_INVALID_EXAMPLES = 4
MAX_VERIFICATION_EXAMPLES = 3

_UNSAFE_TRIGGER_REASON = (
    "Historical evidence is read-only. The isolated collector has a real "
    "MANUAL_REVIEW incident and its current remote code version cannot be "
    "proven without risking that evidence."
)
_PATCH_NOTE = (
    "Bright Data reported a diff and RecallGuard retained the preview verdict, "
    "but the actual scraper source diff was not persisted. The literal patch is "
    "therefore unavailable."
)


def read_live_brightdata_evidence(session: Session) -> LiveBrightDataEvidenceRead:
    """Return historical provider proof for the one isolated healing collector."""
    collector = session.scalar(
        select(Collector).where(
            Collector.provider == LIVE_HEALING_PROVIDER,
            Collector.external_collector_id == LIVE_HEALING_EXTERNAL_COLLECTOR_ID,
        )
    )
    if collector is None:
        return _unavailable(
            "The isolated Bright Data healing collector is not registered in this "
            "database."
        )

    incidents = list(
        session.scalars(
            select(ReliabilityIncident)
            .where(ReliabilityIncident.collector_id == collector.id)
            .order_by(ReliabilityIncident.detected_at.desc())
        )
    )
    incident = next((_ for _ in incidents if _broken_occurrence(_) is not None), None)
    if incident is None:
        return LiveBrightDataEvidenceRead(
            available=False,
            live_trigger_reason=_UNSAFE_TRIGGER_REASON,
            collector=_collector_view(collector),
            repair_patch_note=_PATCH_NOTE,
        )

    occurrence = _broken_occurrence(incident)
    if occurrence is None:  # pragma: no cover - guarded by the selection above
        raise AssertionError("selected incident lost its broken-run occurrence")

    broken_run = session.get(CollectorRun, incident.detection_run_id)
    if broken_run is None:
        return LiveBrightDataEvidenceRead(
            available=False,
            live_trigger_reason=_UNSAFE_TRIGGER_REASON,
            collector=_collector_view(collector),
            repair_patch_note=_PATCH_NOTE,
        )

    verification_event = _last_event(incident, "verification_failed")
    verification_run = _event_run(session, verification_event)

    return LiveBrightDataEvidenceRead(
        available=True,
        live_trigger_reason=_UNSAFE_TRIGGER_REASON,
        collector=_collector_view(collector),
        broken_run=_run_view(broken_run),
        invalid_records=_invalid_examples(occurrence),
        detection=_detection_view(incident, occurrence),
        repair_attempts=_repair_attempts(incident),
        repair_patch_available=False,
        repair_patch_note=_PATCH_NOTE,
        verification=(
            _verification_view(session, incident, verification_run, verification_event)
            if verification_run is not None and verification_event is not None
            else None
        ),
        automation=_automation_accounting(incident, verification_run),
    )


def _unavailable(reason: str) -> LiveBrightDataEvidenceRead:
    return LiveBrightDataEvidenceRead(
        available=False,
        live_trigger_reason=reason,
        repair_patch_note=_PATCH_NOTE,
    )


def _collector_view(collector: Collector) -> LiveEvidenceCollector:
    return LiveEvidenceCollector(
        collector_id=collector.id,
        name=collector.name,
        provider=collector.provider,
        external_collector_id=collector.external_collector_id,
    )


def _run_view(run: CollectorRun) -> LiveEvidenceRun:
    orchestration = _mapping((run.raw_metadata or {}).get("orchestration"))
    return LiveEvidenceRun(
        collector_run_id=run.id,
        provider_job_id=run.external_run_id,
        status=run.status.value,
        started_at=_aware(run.started_at),
        completed_at=_aware(run.completed_at),
        fetched_record_count=_integer(
            orchestration.get("fetched_record_count"), run.record_count
        ),
        valid_record_count=_integer(
            orchestration.get("valid_record_count"), run.record_count
        ),
        invalid_record_count=_integer(orchestration.get("invalid_record_count"), 0),
        accepted_record_count=run.record_count,
    )


def _broken_occurrence(
    incident: ReliabilityIncident,
) -> dict[str, Any] | None:
    for occurrence in _entries(incident.evidence, "occurrences"):
        samples = _dicts(occurrence.get("sample_violations"))
        if any(_is_tam_score_violation(sample) for sample in samples):
            return occurrence
    return None


def _is_tam_score_violation(sample: dict[str, Any]) -> bool:
    raw = _mapping(sample.get("raw"))
    return sample.get("reason") == "invalid_score" and _number(
        raw.get("tam_score")
    ) is not None


def _invalid_examples(occurrence: dict[str, Any]) -> list[LiveEvidenceInvalidRecord]:
    examples: list[LiveEvidenceInvalidRecord] = []
    for sample in _dicts(occurrence.get("sample_violations")):
        if not _is_tam_score_violation(sample):
            continue
        raw = _mapping(sample.get("raw"))
        value = _number(raw.get("tam_score"))
        if value is None:  # pragma: no cover - guarded above
            continue
        examples.append(
            LiveEvidenceInvalidRecord(
                index=sample.get("index") if isinstance(sample.get("index"), int) else None,
                problem=_string(raw.get("problem")),
                field="tam_score",
                value=value,
                allowed_min=1,
                allowed_max=10,
                reason="invalid_score",
                detail=_string(sample.get("detail")),
            )
        )
        if len(examples) == MAX_INVALID_EXAMPLES:
            break
    return examples


def _detection_view(
    incident: ReliabilityIncident, occurrence: dict[str, Any]
) -> LiveEvidenceDetection:
    return LiveEvidenceDetection(
        incident_id=incident.id,
        detected_at=incident.detected_at,
        observed_record_count=_integer(occurrence.get("observed_record_count"), 0),
        field="tam_score",
        classification=_string(occurrence.get("classification"))
        or incident.classification.value,
        severity=_string(occurrence.get("severity")),
        confidence=_number(occurrence.get("confidence")),
        recommended_action=_string(occurrence.get("recommended_action"))
        or incident.recommended_action.value,
    )


def _repair_attempts(
    incident: ReliabilityIncident,
) -> list[LiveEvidenceRepairAttempt]:
    attempts: dict[int, dict[str, Any]] = {}
    for event in _entries(incident.evidence, "events"):
        attempt = event.get("attempt")
        if not isinstance(attempt, int):
            continue
        current = attempts.setdefault(
            attempt,
            {
                "attempt": attempt,
                "status": "started",
                "provider_status": None,
                "has_diff": None,
                "preview_records": None,
                "preview_valid_records": None,
                "preview_invalid_records": None,
                "deployed": False,
                "patch_available": False,
                "before_logic": None,
                "after_logic": None,
                "note": None,
            },
        )
        name = event.get("event")
        if name == "repair_candidate_registered":
            candidate = _mapping(event.get("candidate"))
            current.update(
                status="candidate_registered",
                provider_status=_string(candidate.get("provider_status")),
                has_diff=(
                    candidate.get("has_diff")
                    if isinstance(candidate.get("has_diff"), bool)
                    else None
                ),
                preview_records=_optional_integer(candidate.get("preview_records")),
            )
        elif name == "candidate_approved":
            preflight = _mapping(event.get("preflight"))
            current.update(
                status="deployed",
                provider_status=_string(event.get("provider_status")),
                preview_records=_optional_integer(preflight.get("preview_records")),
                preview_valid_records=_optional_integer(preflight.get("valid_records")),
                preview_invalid_records=_optional_integer(
                    preflight.get("invalid_records")
                ),
                deployed=True,
                note=_string(event.get("note")),
            )
        elif name == "verification_failed":
            current.update(
                status="verification_rejected",
                note="Fresh-run regression verification rejected recovery.",
            )
        elif name == "healing_failed" and event.get("reason") == "candidate_rejected":
            current.update(
                status="candidate_rejected",
                note=_string(event.get("preflight_reason"))
                or "RecallGuard rejected the repair candidate.",
            )
        elif name == "escalation_reason" and current["status"] == "candidate_registered":
            current.update(
                status="manual_review",
                note=_string(event.get("reason")),
            )

    return [LiveEvidenceRepairAttempt(**attempts[key]) for key in sorted(attempts)]


def _verification_view(
    session: Session,
    incident: ReliabilityIncident,
    run: CollectorRun,
    event: dict[str, Any],
) -> LiveEvidenceVerification:
    signals = list(
        session.scalars(
            select(Signal)
            .where(Signal.collector_run_id == run.id)
            .order_by(Signal.created_at, Signal.id)
            .limit(MAX_VERIFICATION_EXAMPLES)
        )
    )
    samples: list[LiveEvidenceVerificationSample] = []
    for signal in signals:
        score = _number((signal.signal_metadata or {}).get("tam_score"))
        if score is None:
            continue
        samples.append(
            LiveEvidenceVerificationSample(problem=signal.title, tam_score=score)
        )

    failed_checks = [
        LiveEvidenceFailedCheck(
            name=_string(check.get("name")) or "unknown",
            expected=_string(check.get("expected")),
            observed=_string(check.get("observed")),
            detail=_string(check.get("detail")),
        )
        for check in _dicts(event.get("failed_checks"))
    ]
    counts = _run_view(run)
    contract_passed = counts.invalid_record_count == 0 and run.status.value == "succeeded"
    return LiveEvidenceVerification(
        run=counts,
        samples=samples,
        contract_validation="PASS" if contract_passed else "FAIL",
        regression_result="FAIL" if failed_checks else "PASS",
        failed_checks=failed_checks,
        final_decision="REJECT" if failed_checks else "APPROVE",
        final_status=incident.status.value,
        recovery_proof=incident.recovery_proof,
    )


def _automation_accounting(
    incident: ReliabilityIncident, verification_run: CollectorRun | None
) -> list[LiveEvidenceAutomationStage]:
    has_approved = _last_event(incident, "candidate_approved") is not None
    return [
        LiveEvidenceAutomationStage(
            stage="BROKEN SCRAPER",
            automation="MANUAL / NOT RETAINED",
            result="DEFECT OBSERVED",
            detail=(
                "Persisted output proves the ×10 score behavior; no scraper source "
                "version or literal patch was retained."
            ),
        ),
        LiveEvidenceAutomationStage(
            stage="RUN",
            automation="AUTOMATED AFTER INVOCATION",
            result="PROVIDER JOB PERSISTED",
            detail="GapRadar triggered, polled, downloaded, and validated the Bright Data job.",
        ),
        LiveEvidenceAutomationStage(
            stage="DETECTION",
            automation="AUTOMATIC",
            result="EXTRACTION_DRIFT",
            detail="Source-contract validation opened the RecallGuard incident.",
        ),
        LiveEvidenceAutomationStage(
            stage="REPAIR + DEPLOY",
            automation="AUTOMATIC AFTER MANUAL SMOKE START",
            result="DEPLOYED" if has_approved else "NOT DEPLOYED",
            detail=(
                "Bright Data self-heal, preview gating, approval, and auto-save ran "
                "through the existing healing orchestrator."
            ),
        ),
        LiveEvidenceAutomationStage(
            stage="FRESH RUN + VERIFY",
            automation="AUTOMATIC",
            result="REGRESSION REJECTED" if verification_run is not None else "NOT RUN",
            detail="A new provider job corrected scores but failed completeness verification.",
        ),
        LiveEvidenceAutomationStage(
            stage="RECOVER",
            automation="NOT ACHIEVED",
            result="MANUAL_REVIEW",
            detail="No recovery proof exists; RecallGuard did not claim self-healing success.",
        ),
    ]


def _last_event(
    incident: ReliabilityIncident, name: str
) -> dict[str, Any] | None:
    matches = [
        event
        for event in _entries(incident.evidence, "events")
        if event.get("event") == name
    ]
    return matches[-1] if matches else None


def _event_run(
    session: Session, event: dict[str, Any] | None
) -> CollectorRun | None:
    if event is None:
        return None
    raw_id = event.get("collector_run_id")
    if isinstance(raw_id, uuid.UUID):
        run_id = raw_id
    elif isinstance(raw_id, str):
        try:
            run_id = uuid.UUID(raw_id)
        except ValueError:
            return None
    else:
        return None
    return session.get(CollectorRun, run_id)


def _entries(evidence: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    return _dicts((evidence or {}).get(key))


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _optional_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
