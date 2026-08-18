from sqlalchemy.orm import Session

from app.db.models import ReliabilityIncident
from app.integrations.brightdata.schemas import HealingRequest
from app.recallguard.prompts import MAX_PROMPT_CHARS, build_heal_prompt
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import evaluate_collector_run
from tests.recallguard.conftest import FakeClock, RunBuilder, invalid_record

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
