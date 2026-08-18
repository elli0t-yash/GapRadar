"""The one deterministic opportunity score, and what it refuses to score."""

import pytest

from app.opportunity_engine.scoring import opportunity_score


def metadata(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "itch_score": 80,
        "severity_score": 8,
        "tam_score": 7,
        "whitespace_score": 6,
        "frequency_score": 5,
    }
    base.update(overrides)
    return base


def test_the_weights_put_every_component_on_the_same_scale() -> None:
    # 0.30*80 + 0.20*80 + 0.20*70 + 0.20*60 + 0.10*50 = 24 + 16 + 14 + 12 + 5
    assert opportunity_score(metadata()) == 71.0


def test_a_perfect_record_scores_one_hundred() -> None:
    assert (
        opportunity_score(
            metadata(
                itch_score=100,
                severity_score=10,
                tam_score=10,
                whitespace_score=10,
                frequency_score=10,
            )
        )
        == 100.0
    )


def test_the_same_input_always_scores_the_same() -> None:
    assert opportunity_score(metadata()) == opportunity_score(metadata())


@pytest.mark.parametrize("field", ["itch_score", "tam_score", "frequency_score"])
def test_a_missing_component_is_unscorable_rather_than_zero(field: str) -> None:
    values = metadata()
    del values[field]

    assert opportunity_score(values) is None


def test_an_out_of_range_tam_score_is_unscorable_and_never_rescaled() -> None:
    """The TAM x10 fault: 70 is refused, not quietly divided by ten.

    A record like this cannot reach a persisted signal -- the source
    adapter rejects it -- but if one ever did, it would surface as
    unscorable instead of being made to look plausible.
    """
    assert opportunity_score(metadata(tam_score=70)) is None


def test_a_string_score_is_not_coerced() -> None:
    assert opportunity_score(metadata(severity_score="8")) is None


def test_a_boolean_is_not_a_score() -> None:
    assert opportunity_score(metadata(frequency_score=True)) is None


def test_a_non_finite_score_is_refused() -> None:
    assert opportunity_score(metadata(itch_score=float("nan"))) is None
    assert opportunity_score(metadata(itch_score=float("inf"))) is None


def test_no_metadata_at_all_is_unscorable() -> None:
    assert opportunity_score(None) is None
    assert opportunity_score({}) is None
