"""Deterministic, isolated RecallGuard self-healing replay.

This module is presentation orchestration for the hackathon demo, not a
second reliability engine. The fixture supplies repeatable field-coverage
snapshots; RecallGuard's existing service owns classification, lifecycle
transitions, attempt accounting, rejection evidence, independent-run checks,
and the final recovery proof.

No function accepts a collector id. Every write is scoped to a disabled
fixture-only collector outside the Bright Data provider namespace, so neither
the healthy product collector nor the real healing collector can be selected
by an API caller. No provider call is made in fixture replay mode.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorRun, ReliabilityIncident, Source
from app.domain.enums import CollectorStatus, RunStatus, SourceType
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    active_incident,
    begin_validation,
    collector_reliability_state,
    evaluate_collector_run,
    record_healing_failure,
    register_repair_candidate,
    start_healing,
    verify_recovery,
)
from app.schemas.reliability import (
    DemoFidelityProof,
    DemoFieldHealth,
    DemoRepairAttempt,
    DemoVerificationResult,
    IncidentEvent,
    RecallGuardDemoRead,
    build_timeline,
)

DEMO_PROVIDER = "fixture_replay"
DEMO_EXTERNAL_COLLECTOR_ID = "gapradar-recallguard-self-healing-v1"
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "demo"
    / "fixtures"
    / "recallguard_self_healing_v1.json"
)
_BASELINE_RECORDS = 100
_BASELINE = BaselineProfile(
    label="recallguard_self_healing_v1_healthy", record_count=_BASELINE_RECORDS
)

_PHASE_HEALTHY = "healthy"
_PHASE_DRIFT = "drift_detected"
_PHASE_HEALING_BAD = "healing_bad_repair"
_PHASE_VERIFYING_BAD = "verifying_bad_repair"
_PHASE_REJECTED_BAD = "rejected_bad_repair"
_PHASE_HEALING_GOOD = "healing_good_repair"
_PHASE_VERIFYING_GOOD = "verifying_good_repair"
_PHASE_SELF_HEALED = "self_healed"

_DISPLAY_STATUS = {
    _PHASE_HEALTHY: "healthy",
    _PHASE_DRIFT: "drift_detected",
    _PHASE_HEALING_BAD: "healing",
    _PHASE_VERIFYING_BAD: "verifying",
    _PHASE_REJECTED_BAD: "rejected",
    _PHASE_HEALING_GOOD: "healing",
    _PHASE_VERIFYING_GOOD: "verifying",
    _PHASE_SELF_HEALED: "self_healed",
}


class DemoNotStartedError(RuntimeError):
    pass


class DemoIsolationError(RuntimeError):
    pass


@lru_cache
def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def read_demo(session: Session) -> RecallGuardDemoRead:
    """Read the latest replay session without changing any state."""
    fixture = _fixture()
    collector = _demo_collector(session)
    if collector is None:
        return _empty_view()

    run, marker = _latest_demo_run(session, collector_id=collector.id)
    if run is None or marker is None:
        return _empty_view(collector=collector)

    session_id = _as_uuid(marker.get("session_id"))
    if session_id is None:
        return _empty_view(collector=collector)
    incident = _incident_for_session(
        session, collector_id=collector.id, session_id=session_id
    )
    demo = _demo_evidence(incident) if incident is not None else marker
    phase = str(demo.get("phase") or _PHASE_HEALTHY)
    coverage_key = str(demo.get("coverage_key") or "healthy")
    classification = fixture["classification"]
    started_at = _as_datetime(demo.get("started_at")) or run.started_at
    updated_at = _as_datetime(demo.get("updated_at")) or run.completed_at

    timeline = []
    if started_at is not None:
        timeline.append(
            IncidentEvent(
                at=started_at,
                event="collector_healthy",
                collector_run_id=run.id if incident is None else None,
                detail="verified fixture baseline",
            )
        )
    if incident is not None:
        timeline.extend(build_timeline(incident))
        timeline.sort(key=lambda entry: entry.at)

    proof = _demo_proof(incident)
    return RecallGuardDemoRead(
        scenario=fixture["scenario"],
        mode=fixture["mode"],
        session_id=session_id,
        collector_id=collector.id,
        collector_name=collector.name,
        provider=collector.provider,
        external_collector_id=collector.external_collector_id,
        status=_DISPLAY_STATUS.get(phase, phase),
        core_status=collector_reliability_state(
            session, collector_id=collector.id
        ).value,
        terminal=phase == _PHASE_SELF_HEALED,
        incident_id=incident.id if incident is not None else None,
        classification=(incident.classification.value if incident is not None else None),
        severity=classification["severity"] if incident is not None else None,
        confidence=classification["confidence"] if incident is not None else None,
        recommended_action=(
            incident.recommended_action.value if incident is not None else None
        ),
        affected_fields=(
            classification["affected_fields"] if incident is not None else []
        ),
        field_health=_field_health(coverage_key, phase=phase),
        repair_attempts=_repair_attempts(demo),
        timeline=timeline,
        proof=proof,
        started_at=started_at,
        updated_at=updated_at,
    )


def start_demo(session: Session) -> RecallGuardDemoRead:
    """Start a new healthy replay, or resume an interrupted isolated one."""
    collector = _ensure_demo_collector(session)
    unresolved = active_incident(session, collector_id=collector.id)
    if unresolved is not None:
        if _demo_evidence(unresolved):
            return read_demo(session)
        raise DemoIsolationError(
            "the isolated demo collector has a non-demo incident; refusing to mutate it"
        )

    session_id = uuid.uuid4()
    at = datetime.now(UTC)
    run = _create_run(
        session,
        collector=collector,
        session_id=session_id,
        phase=_PHASE_HEALTHY,
        record_count=_BASELINE_RECORDS,
        at=at,
    )
    evaluation = evaluate_collector_run(
        session, run=run, baseline=_BASELINE, now=lambda: at
    )
    if not evaluation.passed:  # pragma: no cover - fixture invariant
        raise RuntimeError("healthy demo fixture failed RecallGuard evaluation")
    return read_demo(session)


def advance_demo(session: Session) -> RecallGuardDemoRead:
    """Advance exactly one persisted transition in the deterministic replay."""
    state = read_demo(session)
    if state.session_id is None or state.collector_id is None:
        raise DemoNotStartedError("start the RecallGuard demo before advancing it")
    if state.terminal:
        return state

    collector = session.get(Collector, state.collector_id)
    if collector is None:  # pragma: no cover - referential integrity
        raise DemoNotStartedError("the RecallGuard demo collector no longer exists")
    incident = _incident_for_session(
        session, collector_id=collector.id, session_id=state.session_id
    )
    phase = _phase(incident, session, collector.id)
    at = datetime.now(UTC)

    if phase == _PHASE_HEALTHY:
        degraded_run = _create_run(
            session,
            collector=collector,
            session_id=state.session_id,
            phase=_PHASE_DRIFT,
            record_count=20,
            at=at,
        )
        evaluation = evaluate_collector_run(
            session, run=degraded_run, baseline=_BASELINE, now=lambda: at
        )
        incident = session.get(ReliabilityIncident, evaluation.incident_id)
        if incident is None:  # pragma: no cover - service invariant
            raise RuntimeError("RecallGuard did not persist the detected incident")
        _write_demo_evidence(
            session,
            incident,
            state.session_id,
            phase=_PHASE_DRIFT,
            coverage_key="degraded",
            started_at=state.started_at or at,
            repairs=[],
            at=at,
        )

    elif phase == _PHASE_DRIFT:
        incident = _require_incident(incident)
        start_healing(session, incident, now=lambda: at)
        _write_demo_evidence(
            session,
            incident,
            state.session_id,
            phase=_PHASE_HEALING_BAD,
            coverage_key="degraded",
            started_at=state.started_at or at,
            repairs=[{"attempt": 1, "status": "healing"}],
            at=at,
        )

    elif phase == _PHASE_HEALING_BAD:
        incident = _require_incident(incident)
        _begin_fixture_verification(
            session,
            collector=collector,
            incident=incident,
            session_id=state.session_id,
            repair_index=0,
            phase=_PHASE_VERIFYING_BAD,
            coverage_key="bad_repair",
            started_at=state.started_at or at,
            at=at,
        )

    elif phase == _PHASE_VERIFYING_BAD:
        incident = _require_incident(incident)
        checks = _coverage_checks("bad_repair")
        failures = [check for check in checks if check.status == "fail"]
        if not failures:  # pragma: no cover - fixture invariant
            raise RuntimeError("bad repair fixture did not trigger the regression guard")
        record_healing_failure(
            session,
            incident,
            reason="regression_guard_failed",
            evidence={
                "regression_failures": [check.model_dump(mode="json") for check in failures],
                "verification_run_id": _current_verification_run_id(incident),
            },
            now=lambda: at,
        )
        _write_demo_evidence(
            session,
            incident,
            state.session_id,
            phase=_PHASE_REJECTED_BAD,
            coverage_key="bad_repair",
            started_at=state.started_at or at,
            repairs=[{"attempt": 1, "status": "rejected"}],
            at=at,
        )

    elif phase == _PHASE_REJECTED_BAD:
        incident = _require_incident(incident)
        start_healing(session, incident, now=lambda: at)
        _write_demo_evidence(
            session,
            incident,
            state.session_id,
            phase=_PHASE_HEALING_GOOD,
            coverage_key="degraded",
            started_at=state.started_at or at,
            repairs=[
                {"attempt": 1, "status": "rejected"},
                {"attempt": 2, "status": "healing"},
            ],
            at=at,
        )

    elif phase == _PHASE_HEALING_GOOD:
        incident = _require_incident(incident)
        _begin_fixture_verification(
            session,
            collector=collector,
            incident=incident,
            session_id=state.session_id,
            repair_index=1,
            phase=_PHASE_VERIFYING_GOOD,
            coverage_key="good_repair",
            started_at=state.started_at or at,
            at=at,
        )

    elif phase == _PHASE_VERIFYING_GOOD:
        incident = _require_incident(incident)
        checks = _coverage_checks("good_repair")
        if any(check.status == "fail" for check in checks):  # pragma: no cover
            raise RuntimeError("good repair fixture failed the regression guard")
        verification_run = session.get(
            CollectorRun, uuid.UUID(_current_verification_run_id(incident))
        )
        if verification_run is None:  # pragma: no cover - referential integrity
            raise RuntimeError("demo verification run no longer exists")
        evaluation = verify_recovery(
            session,
            incident,
            verification_run=verification_run,
            baseline=_BASELINE,
            now=lambda: at,
        )
        if not evaluation.passed:  # pragma: no cover - fixture invariant
            raise RuntimeError("good repair did not pass RecallGuard verification")
        proof = dict(incident.recovery_proof or {})
        passed_names = {check.name for check in evaluation.checks if check.passed}
        proof["demo_fidelity"] = {
            "schema_fidelity": (
                "PASS" if "source_contract" in passed_names else "FAIL"
            ),
            "semantic_fidelity": "PASS",
            "source_fidelity": (
                "PASS"
                if {"transport_payload", "ingestion"}.issubset(passed_names)
                else "FAIL"
            ),
            "decision": "APPROVE",
        }
        incident.recovery_proof = proof
        _write_demo_evidence(
            session,
            incident,
            state.session_id,
            phase=_PHASE_SELF_HEALED,
            coverage_key="good_repair",
            started_at=state.started_at or at,
            repairs=[
                {"attempt": 1, "status": "rejected"},
                {"attempt": 2, "status": "approved"},
            ],
            at=at,
        )

    else:  # pragma: no cover - persisted-state corruption
        raise RuntimeError(f"unknown RecallGuard demo phase: {phase}")

    return read_demo(session)


def _ensure_demo_collector(session: Session) -> Collector:
    fixture = _fixture()["collector"]
    collector = _demo_collector(session)
    if collector is not None:
        return collector

    source = session.execute(
        select(Source).where(Source.name == fixture["source_name"])
    ).scalars().first()
    if source is None:
        source = Source(
            name=fixture["source_name"],
            source_type=SourceType.OTHER,
            base_url=fixture["source_url"],
            active=False,
        )
        session.add(source)
        session.flush()

    collector = Collector(
        source_id=source.id,
        provider=DEMO_PROVIDER,
        external_collector_id=DEMO_EXTERNAL_COLLECTOR_ID,
        name=fixture["name"],
        # The replay is advanced only by /reliability/demo. Keeping this
        # collector disabled prevents production collection jobs from ever
        # treating the synthetic identity as runnable provider work.
        status=CollectorStatus.DISABLED,
    )
    session.add(collector)
    session.commit()
    session.refresh(collector)
    return collector


def _demo_collector(session: Session) -> Collector | None:
    return session.execute(
        select(Collector).where(
            Collector.provider == DEMO_PROVIDER,
            Collector.external_collector_id == DEMO_EXTERNAL_COLLECTOR_ID,
        )
    ).scalar_one_or_none()


def _create_run(
    session: Session,
    *,
    collector: Collector,
    session_id: uuid.UUID,
    phase: str,
    record_count: int,
    at: datetime,
) -> CollectorRun:
    run = CollectorRun(
        collector_id=collector.id,
        external_run_id=f"demo-{session_id}-{phase}",
        status=RunStatus.SUCCEEDED,
        started_at=at,
        completed_at=at + timedelta(microseconds=1),
        record_count=record_count,
        raw_metadata={
            "provider": {"mode": "fixture_replay"},
            "orchestration": {
                "stage": "completed",
                "fetched_record_count": record_count,
                "valid_record_count": record_count,
                "invalid_record_count": 0,
                "source_duplicate_count": 0,
                "ingestion": {"accepted": record_count, "duplicates": 0},
            },
            "recallguard_demo": {
                "scenario": _fixture()["scenario"],
                "session_id": str(session_id),
                "phase": phase,
                "started_at": at.isoformat(),
                "updated_at": at.isoformat(),
            },
        },
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _begin_fixture_verification(
    session: Session,
    *,
    collector: Collector,
    incident: ReliabilityIncident,
    session_id: uuid.UUID,
    repair_index: int,
    phase: str,
    coverage_key: str,
    started_at: datetime,
    at: datetime,
) -> None:
    repair = _fixture()["repairs"][repair_index]
    register_repair_candidate(
        session,
        incident,
        candidate={
            "mode": "fixture_replay",
            "label": repair["label"],
            "changes": repair["changes"],
        },
        now=lambda: at,
    )
    verification_at = at + timedelta(microseconds=1)
    run = _create_run(
        session,
        collector=collector,
        session_id=session_id,
        phase=phase,
        record_count=_BASELINE_RECORDS,
        at=verification_at,
    )
    begin_validation(
        session,
        incident,
        verification_run=run,
        now=lambda: verification_at + timedelta(microseconds=1),
    )
    repairs = [
        {"attempt": item.attempt, "status": item.status}
        for item in _repair_attempts(_demo_evidence(incident))
    ]
    repairs = [item for item in repairs if item["attempt"] != repair["attempt"]]
    repairs.append({"attempt": repair["attempt"], "status": "verifying"})
    repairs.sort(key=lambda item: item["attempt"])
    _write_demo_evidence(
        session,
        incident,
        session_id,
        phase=phase,
        coverage_key=coverage_key,
        started_at=started_at,
        repairs=repairs,
        at=verification_at,
        verification_run_id=run.id,
    )


def _write_demo_evidence(
    session: Session,
    incident: ReliabilityIncident,
    session_id: uuid.UUID,
    *,
    phase: str,
    coverage_key: str,
    started_at: datetime,
    repairs: list[dict[str, Any]],
    at: datetime,
    verification_run_id: uuid.UUID | None = None,
) -> None:
    evidence = dict(incident.evidence or {})
    previous = _demo_evidence(incident)
    evidence["demo"] = {
        "scenario": _fixture()["scenario"],
        "mode": _fixture()["mode"],
        "session_id": str(session_id),
        "phase": phase,
        "coverage_key": coverage_key,
        "repairs": repairs,
        "started_at": started_at.isoformat(),
        "updated_at": at.isoformat(),
        "current_verification_run_id": (
            str(verification_run_id)
            if verification_run_id is not None
            else previous.get("current_verification_run_id")
        ),
    }
    incident.evidence = evidence
    session.commit()
    session.refresh(incident)


def _field_health(coverage_key: str, *, phase: str) -> list[DemoFieldHealth]:
    coverage = _fixture()["coverage"]
    baseline = coverage["healthy"]
    current = coverage[coverage_key]
    regression = phase in {_PHASE_VERIFYING_BAD, _PHASE_REJECTED_BAD}
    rows = []
    for field, baseline_pct in baseline.items():
        current_pct = current[field]
        drop = baseline_pct - current_pct
        rows.append(
            DemoFieldHealth(
                field=field,
                baseline_pct=baseline_pct,
                current_pct=current_pct,
                drop_pct=drop if drop > 0 else None,
                status=(
                    "regression"
                    if regression and drop > 0
                    else "drift"
                    if drop > 0
                    else "healthy"
                ),
            )
        )
    return rows


def _coverage_checks(coverage_key: str) -> list[DemoVerificationResult]:
    coverage = _fixture()["coverage"]
    before = coverage["degraded"]
    after = coverage[coverage_key]
    baseline = coverage["healthy"]
    return [
        DemoVerificationResult(
            field=field,
            before_pct=before[field],
            after_pct=after[field],
            status="pass" if after[field] >= baseline[field] else "fail",
        )
        for field in baseline
    ]


def _repair_attempts(demo: dict[str, Any]) -> list[DemoRepairAttempt]:
    statuses = {
        item.get("attempt"): item.get("status")
        for item in demo.get("repairs", [])
        if isinstance(item, dict)
    }
    attempts = []
    for repair in _fixture()["repairs"]:
        status = statuses.get(repair["attempt"])
        if status is None:
            continue
        attempts.append(
            DemoRepairAttempt(
                attempt=repair["attempt"],
                label=repair["label"],
                status=status,
                changes=repair["changes"],
                verification=(
                    _coverage_checks(repair["coverage_key"])
                    if status in {"verifying", "rejected", "approved"}
                    else []
                ),
            )
        )
    return attempts


def _empty_view(collector: Collector | None = None) -> RecallGuardDemoRead:
    fixture = _fixture()
    identity = fixture["collector"]
    return RecallGuardDemoRead(
        scenario=fixture["scenario"],
        mode=fixture["mode"],
        collector_id=collector.id if collector is not None else None,
        collector_name=collector.name if collector is not None else identity["name"],
        provider=collector.provider if collector is not None else identity["provider"],
        external_collector_id=(
            collector.external_collector_id
            if collector is not None
            else identity["external_collector_id"]
        ),
        status="healthy",
        core_status="healthy",
        field_health=_field_health("healthy", phase=_PHASE_HEALTHY),
    )


def _latest_demo_run(
    session: Session, *, collector_id: uuid.UUID
) -> tuple[CollectorRun | None, dict[str, Any] | None]:
    runs = session.execute(
        select(CollectorRun)
        .where(CollectorRun.collector_id == collector_id)
        .order_by(CollectorRun.created_at.desc(), CollectorRun.started_at.desc())
    ).scalars()
    for run in runs:
        marker = (run.raw_metadata or {}).get("recallguard_demo")
        if isinstance(marker, dict):
            return run, marker
    return None, None


def _incident_for_session(
    session: Session, *, collector_id: uuid.UUID, session_id: uuid.UUID
) -> ReliabilityIncident | None:
    incidents = session.execute(
        select(ReliabilityIncident)
        .where(ReliabilityIncident.collector_id == collector_id)
        .order_by(ReliabilityIncident.detected_at.desc())
    ).scalars()
    for incident in incidents:
        if _demo_evidence(incident).get("session_id") == str(session_id):
            return incident
    return None


def _phase(
    incident: ReliabilityIncident | None, session: Session, collector_id: uuid.UUID
) -> str:
    if incident is not None:
        return str(_demo_evidence(incident).get("phase"))
    _, marker = _latest_demo_run(session, collector_id=collector_id)
    return str((marker or {}).get("phase") or _PHASE_HEALTHY)


def _demo_evidence(incident: ReliabilityIncident | None) -> dict[str, Any]:
    if incident is None:
        return {}
    demo = (incident.evidence or {}).get("demo")
    return demo if isinstance(demo, dict) else {}


def _current_verification_run_id(incident: ReliabilityIncident) -> str:
    value = _demo_evidence(incident).get("current_verification_run_id")
    if not isinstance(value, str):
        raise TypeError("demo verification run id was not persisted")
    return value


def _demo_proof(incident: ReliabilityIncident | None) -> DemoFidelityProof | None:
    if incident is None:
        return None
    value = (incident.recovery_proof or {}).get("demo_fidelity")
    return DemoFidelityProof.model_validate(value) if isinstance(value, dict) else None


def _require_incident(
    incident: ReliabilityIncident | None,
) -> ReliabilityIncident:
    if incident is None:
        raise RuntimeError("demo incident was not persisted")
    return incident


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
