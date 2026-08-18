from app.domain.enums import SignalType


def test_signal_type_has_problem() -> None:
    assert SignalType.PROBLEM.value == "problem"
    assert SignalType("problem") is SignalType.PROBLEM


def test_signal_type_has_research() -> None:
    assert SignalType.RESEARCH.value == "research"
    assert SignalType("research") is SignalType.RESEARCH


def test_pre_existing_signal_type_values_are_unchanged() -> None:
    # Adding a taxonomy value must never renumber, rename, or drop an
    # existing one: these strings are persisted (via the member name) and
    # exposed over the API (via the value).
    assert SignalType.COMPLAINT.value == "complaint"
    assert SignalType.QUESTION.value == "question"
    assert SignalType.FEATURE_REQUEST.value == "feature_request"
    assert SignalType.REVIEW.value == "review"
    assert SignalType.OTHER.value == "other"


def test_signal_type_membership_is_exactly_the_expected_set() -> None:
    assert {member.name: member.value for member in SignalType} == {
        "COMPLAINT": "complaint",
        "QUESTION": "question",
        "FEATURE_REQUEST": "feature_request",
        "REVIEW": "review",
        "PROBLEM": "problem",
        "RESEARCH": "research",
        "OTHER": "other",
    }


def test_signal_type_names_fit_the_persisted_column_width() -> None:
    # signals.signal_type is VARCHAR(32) and SQLAlchemy's non-native Enum
    # persists the member NAME, so a name longer than 32 characters would
    # fail to insert.
    assert max(len(member.name) for member in SignalType) <= 32
