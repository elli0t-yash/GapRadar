"""Investigation -> ResearchSubject, and the Signal path beside it.

The two conversions are tested together on purpose: the property that
matters is not that either one works, but that they produce the SAME
kind of object with a DIFFERENT origin, which is what lets one engine
serve both without confusing them.
"""

from sqlalchemy.orm import Session

from app.db.models import Signal
from app.domain.enums import ResearchSubjectOrigin
from app.investigations.schemas import InvestigationCreate
from app.investigations.service import create_investigation
from app.investigations.subject import research_subject_from_investigation
from app.research_intelligence.schemas import ResearchSubject
from app.research_intelligence.service import research_subject_from_signal


def test_an_investigation_becomes_a_subject_labelled_investigation(
    db_session: Session, investigation
) -> None:
    """The label is the whole point: it is what keeps the two apart."""
    subject = research_subject_from_investigation(investigation)
    assert subject.origin is ResearchSubjectOrigin.INVESTIGATION


def test_a_signal_becomes_a_subject_labelled_signal(
    opportunity_signal: Signal,
) -> None:
    subject = research_subject_from_signal(opportunity_signal)
    assert subject.origin is ResearchSubjectOrigin.SIGNAL


def test_both_conversions_produce_the_same_type(
    db_session: Session, investigation, opportunity_signal: Signal
) -> None:
    """One engine, two subjects -- so both must arrive as one type."""
    assert isinstance(research_subject_from_investigation(investigation), ResearchSubject)
    assert isinstance(research_subject_from_signal(opportunity_signal), ResearchSubject)


def test_the_investigation_id_becomes_the_subject_id(
    db_session: Session, investigation
) -> None:
    assert research_subject_from_investigation(investigation).subject_id == (
        investigation.id
    )


def test_the_users_query_is_the_problem_verbatim(
    db_session: Session, investigation
) -> None:
    """No derived canonical problem exists, so none is invented."""
    subject = research_subject_from_investigation(investigation)
    assert subject.problem == investigation.query


def test_the_description_falls_back_to_the_query(
    db_session: Session, investigation
) -> None:
    """Repeating what the user said beats inventing what they did not.

    The description is NOT persisted onto the row and is not presented
    as something the user wrote -- it exists because the research
    contract wants elaboration text and there is none.
    """
    assert investigation.description is None
    subject = research_subject_from_investigation(investigation)
    assert subject.description == investigation.query


def test_a_derived_description_is_used_when_one_exists(
    db_session: Session, investigation
) -> None:
    investigation.description = "Referral handoffs are lost between systems."
    db_session.commit()

    subject = research_subject_from_investigation(investigation)

    assert subject.description == "Referral handoffs are lost between systems."


def test_the_industry_passes_through(db_session: Session, investigation) -> None:
    assert research_subject_from_investigation(investigation).industry == "Logistics"


def test_a_missing_industry_stays_missing(db_session: Session) -> None:
    """Never an invented one."""
    investigation = create_investigation(
        db_session, payload=InvestigationCreate(query="rota swaps are chaotic")
    )
    assert research_subject_from_investigation(investigation).industry is None


def test_converting_an_investigation_writes_nothing(
    db_session: Session, investigation
) -> None:
    """A pure function: no Signal, no row, no side effect anywhere.

    This is the test that fails if someone ever "reuses" the research
    code by inserting the investigation into `signals` first.
    """
    from sqlalchemy import func, select

    research_subject_from_investigation(investigation)

    assert db_session.execute(
        select(func.count()).select_from(Signal)
    ).scalar_one() == 0
