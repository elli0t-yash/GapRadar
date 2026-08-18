from datetime import timedelta

from sqlalchemy.orm import Session

from app.db.models import ReliabilityIncident
from app.domain.enums import IncidentStatus
from app.integrations.brightdata.schemas import HealingRequest
from app.recallguard.prompts import MAX_PROMPT_CHARS, build_heal_prompt
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import (
    evaluate_collector_run,
    register_repair_candidate,
    start_healing,
    verify_recovery,
)
from tests.recallguard.conftest import (
    DETECTED_AT,
    FakeClock,
    RunBuilder,
    invalid_record,
)

BASELINE = BaselineProfile(label="fix_my_itch_healthy_v1", record_count=133)


def tam_incident(db_session: Session, runs: RunBuilder) -> ReliabilityIncident:
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[
                invalid_record(index=0, tam_score=60),
                invalid_record(index=4, tam_score=90),
            ],
            fetched=133,
        ),
        baseline=BASELINE,
        now=FakeClock(),
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None
    return incident


def test_tam_prompt_states_the_contract_and_the_wrong_values(
    db_session: Session, runs: RunBuilder
) -> None:
    prompt = build_heal_prompt(tam_incident(db_session, runs))

    assert "https://razorpay.com/m/fix-my-itch/" in prompt
    assert "extraction_drift" in prompt
    # The contract, stated from the source model itself.
    assert "tam_score returned 60 but must be at most 10" in prompt
    assert "tam_score returned 90 but must be at most 10" in prompt
    # The repair instruction, and the trap it must not fall into.
    assert "Extract the values displayed on the page." in prompt
    assert "Never rescale, round, or otherwise transform them." in prompt
    assert "Keep the existing output schema" in prompt


def test_prompt_never_instructs_a_silent_rescale(
    db_session: Session, runs: RunBuilder
) -> None:
    prompt = build_heal_prompt(tam_incident(db_session, runs)).lower()

    for forbidden in ("divide", "/ 10", "scale down", "convert 60 to 6"):
        assert forbidden not in prompt


def test_prompt_carries_no_credentials_or_environment_values(
    db_session: Session, runs: RunBuilder
) -> None:
    prompt = build_heal_prompt(tam_incident(db_session, runs))

    for forbidden in ("Bearer", "Authorization", "API_KEY", "token", "postgresql"):
        assert forbidden not in prompt


def test_prompt_fits_the_provider_limit(db_session: Session, runs: RunBuilder) -> None:
    # Many violations of several kinds, to push the builder past the cap.
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[
                invalid_record(index=i, tam_score=60 + i, problem="P" * 400)
                for i in range(5)
            ],
            fetched=133,
        ),
        baseline=BASELINE,
        now=FakeClock(),
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None

    prompt = build_heal_prompt(incident)

    assert len(prompt) <= MAX_PROMPT_CHARS
    # Still a usable request for the provider.
    assert HealingRequest(collector_id="c_1", prompt=prompt).prompt == prompt
    assert prompt.startswith("The scraper for")


def test_prompt_is_deterministic(db_session: Session, runs: RunBuilder) -> None:
    incident = tam_incident(db_session, runs)

    assert build_heal_prompt(incident) == build_heal_prompt(incident)


def test_completeness_collapse_prompt_reports_expected_and_observed(
    db_session: Session, runs: RunBuilder
) -> None:
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.succeeded(record_count=0),
        baseline=BASELINE,
        now=FakeClock(),
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None

    prompt = build_heal_prompt(incident)

    assert "completeness" in prompt
    assert "0 records" in prompt
    assert len(prompt) <= MAX_PROMPT_CHARS


# --- the newest failure wins ------------------------------------------------
#
# Incident ae20c718-55b9-4fa3-9bd9-31b78f23495e, in full: TAM corruption
# opened it, attempt 2's repair fixed the values and shipped
# `categories.slice(0, 1)` doing it, and the verification run came back
# structurally clean but one category deep. The attempt-3 prompt has to
# be about the coverage regression, not about tam_score.


def coverage_regressed_incident(
    db_session: Session,
    runs: RunBuilder,
    *,
    baseline: BaselineProfile = BASELINE,
    verified_record_count: int = 10,
) -> ReliabilityIncident:
    """Drive the real lifecycle to the state the live incident is in.

    Detection, a repair attempt, an approved candidate, then a fresh run
    that satisfies every source-contract check and still returns far too
    few records -- so the newest recorded failure is a completeness
    failure carrying no field violations at all.
    """
    clock = FakeClock()
    evaluation = evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(index=0, tam_score=60)], fetched=133
        ),
        baseline=baseline,
        now=clock,
    )
    incident = db_session.get(ReliabilityIncident, evaluation.incident_id)
    assert incident is not None

    start_healing(db_session, incident, now=clock)
    register_repair_candidate(db_session, incident, now=clock)

    verification_run = runs.succeeded(
        record_count=verified_record_count,
        started_at=DETECTED_AT + timedelta(hours=1),
    )
    verify_recovery(
        db_session,
        incident,
        verification_run=verification_run,
        baseline=baseline,
        now=clock,
    )
    assert incident.status is IncidentStatus.DEGRADED
    return incident


def test_a_later_verification_failure_replaces_the_stale_diagnosis(
    db_session: Session, runs: RunBuilder
) -> None:
    """The bug this fixes: attempt 3 must not re-ask for the TAM repair."""
    incident = coverage_regressed_incident(db_session, runs)

    prompt = build_heal_prompt(incident)

    assert "completeness" in prompt
    # The values are correct now. Nothing may suggest otherwise.
    assert "tam_score returned" not in prompt
    assert "Wrong values returned:" not in prompt
    assert "source_contract" not in prompt


def test_the_completeness_prompt_quotes_the_recorded_numbers(
    db_session: Session, runs: RunBuilder
) -> None:
    incident = coverage_regressed_incident(db_session, runs)

    prompt = build_heal_prompt(incident)

    assert "at most a 50% drop from baseline fix_my_itch_healthy_v1 (133 records)" in (
        prompt
    )
    assert "10 records (92% drop)" in prompt


def test_the_completeness_prompt_forbids_shipping_a_preview_subset(
    db_session: Session, runs: RunBuilder
) -> None:
    """The actual defect, described without naming the site's categories."""
    incident = coverage_regressed_incident(db_session, runs)

    prompt = build_heal_prompt(incident)

    assert "Restore full category traversal and collection coverage." in prompt
    assert (
        "Do not restrict production collection to a preview subset or first category."
        in prompt
    )


def test_the_completeness_prompt_keeps_the_schema_and_value_rules(
    db_session: Session, runs: RunBuilder
) -> None:
    """A coverage repair must not become a licence to change values."""
    incident = coverage_regressed_incident(db_session, runs)

    prompt = build_heal_prompt(incident)

    assert "Extract the values displayed on the page." in prompt
    assert "Never rescale, round, or otherwise transform them." in prompt
    assert "Keep the existing output schema and field names unchanged." in prompt
    # And the ranges the current extraction is already satisfying, read
    # off the source model rather than restated.
    assert "Preserve the current valid extraction:" in prompt
    assert "itch_score 0-100" in prompt
    assert "severity_score, tam_score, whitespace_score, frequency_score 1-10" in prompt


def test_the_completeness_prompt_fits_the_provider_limit(
    db_session: Session, runs: RunBuilder
) -> None:
    incident = coverage_regressed_incident(db_session, runs)

    prompt = build_heal_prompt(incident)

    assert len(prompt) <= MAX_PROMPT_CHARS
    assert HealingRequest(collector_id="c_1", prompt=prompt).prompt == prompt


def test_no_production_specific_volume_is_hardcoded(
    db_session: Session, runs: RunBuilder
) -> None:
    """Change the baseline and the observation, and the prompt follows.

    Nothing about this source's current size or category list may be
    baked into the builder: a prompt that hardcoded 133 would go stale
    the moment the source grew.
    """
    other_baseline = BaselineProfile(label="other_source_v1", record_count=500)
    incident = coverage_regressed_incident(
        db_session, runs, baseline=other_baseline, verified_record_count=7
    )

    prompt = build_heal_prompt(incident)

    assert "500 records" in prompt
    assert "7 records (99% drop)" in prompt
    assert "133" not in prompt
    assert "fix_my_itch_healthy_v1" not in prompt
    # The coverage instruction is generic in both directions.
    assert "B2B Services" not in prompt
    assert "categories.slice" not in prompt


def test_an_incident_with_no_verification_failure_keeps_the_original_diagnosis(
    db_session: Session, runs: RunBuilder
) -> None:
    """Requirement 8: nothing changes for a plain detection failure."""
    incident = tam_incident(db_session, runs)

    prompt = build_heal_prompt(incident)

    assert "tam_score returned 60 but must be at most 10" in prompt
    # No coverage advice, because completeness never failed.
    assert "Restore full category traversal" not in prompt
    assert "Preserve the current valid extraction:" not in prompt


def test_an_older_verification_failure_does_not_override_a_newer_detection(
    db_session: Session, runs: RunBuilder
) -> None:
    """Recency decides, in both directions.

    After the coverage regression is recorded, a fresh detection run
    finds the values corrupted again. That occurrence is newer, so it --
    not the older verification failure -- is what the next prompt is
    about.
    """
    incident = coverage_regressed_incident(db_session, runs)
    clock = FakeClock(start=DETECTED_AT + timedelta(days=1))

    evaluate_collector_run(
        db_session,
        run=runs.source_validation_failed(
            invalid_records=[invalid_record(index=0, tam_score=88)],
            fetched=133,
            started_at=DETECTED_AT + timedelta(days=1),
        ),
        baseline=BASELINE,
        now=clock,
    )
    db_session.refresh(incident)

    prompt = build_heal_prompt(incident)

    assert "tam_score returned 88 but must be at most 10" in prompt
    assert "Restore full category traversal" not in prompt
