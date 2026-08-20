"""One paper, two subjects, two independent verdicts.

The requirement this file exists for: an Investigation and a Signal may
both be researched, may both surface the SAME arXiv paper, and must end
up with one paper row and two separate judgements that cannot overwrite
one another. Getting that wrong in either direction is bad -- duplicated
papers destroy global dedupe, shared verdicts destroy provenance.

No network: acquisition is replayed from the committed records.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Investigation,
    InvestigationResearchMatch,
    OpportunityResearchMatch,
    ResearchPaper,
    ResearchSearchRun,
    Signal,
)
from app.domain.enums import ResearchSubjectOrigin
from app.investigations.subject import research_subject_from_investigation
from app.research_intelligence.acquisition import SequenceResearchCollector
from app.research_intelligence.matching import ResearchMatchVerdict
from app.research_intelligence.orchestration import research_subject
from app.research_intelligence.persistence import (
    match_store_for,
    search_run_attribution,
)
from app.research_intelligence.query_generation import (
    ConceptQueryGenerator,
    ResearchQueryPlan,
)
from app.research_intelligence.schemas import ResearchSubject
from app.research_intelligence.service import (
    get_subject_research_intelligence,
    research_subject_from_signal,
)

SEARCHED_AT = datetime(2026, 8, 20, tzinfo=UTC)


class ScoringMatcher:
    """Judges everything at a fixed score, so verdicts are identifiable."""

    def __init__(self, score: float, reason: str) -> None:
        self.score = score
        self.reason = reason

    def judge(
        self, *, subject: ResearchSubject, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> ResearchMatchVerdict | None:
        return ResearchMatchVerdict(
            relevance_score=self.score, match_reason=self.reason
        )


def collector_for(subject: ResearchSubject, records: list[dict[str, Any]]):
    plan = ConceptQueryGenerator().generate(subject)
    return SequenceResearchCollector(
        {query: records for query in plan.queries}, searched_at=SEARCHED_AT
    )


def research(
    session: Session, subject: ResearchSubject, records: list[dict[str, Any]], matcher
):
    return research_subject(
        session,
        subject=subject,
        collector=collector_for(subject, records),
        matcher=matcher,
    )


# -- the seam itself --------------------------------------------------------


def test_a_signal_subject_is_attributed_to_the_signal_column() -> None:
    subject = ResearchSubject(
        subject_id=__import__("uuid").uuid4(),
        origin=ResearchSubjectOrigin.SIGNAL,
        problem="p",
        description="d",
    )
    assert search_run_attribution(subject) == {
        "signal_id": subject.subject_id,
        "investigation_id": None,
    }


def test_an_investigation_subject_is_attributed_to_the_investigation_column() -> None:
    subject = ResearchSubject(
        subject_id=__import__("uuid").uuid4(),
        origin=ResearchSubjectOrigin.INVESTIGATION,
        problem="p",
        description="d",
    )
    assert search_run_attribution(subject) == {
        "signal_id": None,
        "investigation_id": subject.subject_id,
    }


def test_the_store_is_chosen_by_the_subject_not_the_caller() -> None:
    """A caller cannot hand the engine a mismatched pair, because it never
    chooses one: `match_store_for` reads the origin off the subject."""
    signal_subject = ResearchSubject(
        subject_id=__import__("uuid").uuid4(),
        origin=ResearchSubjectOrigin.SIGNAL,
        problem="p",
        description="d",
    )
    investigation_subject = signal_subject.model_copy(
        update={"origin": ResearchSubjectOrigin.INVESTIGATION}
    )

    assert match_store_for(signal_subject) is not match_store_for(
        investigation_subject
    )


# -- coexistence ------------------------------------------------------------


def test_the_same_paper_is_stored_once_across_both_subjects(
    db_session: Session,
    investigation: Investigation,
    opportunity_signal: Signal,
    records: list[dict[str, Any]],
) -> None:
    """GLOBAL DEDUPE BY ARXIV_ID. A paper is an entity, not an observation."""
    research(
        db_session,
        research_subject_from_signal(opportunity_signal),
        records,
        ScoringMatcher(80.0, "signal verdict"),
    )
    after_signal = db_session.execute(
        select(func.count()).select_from(ResearchPaper)
    ).scalar_one()

    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(90.0, "investigation verdict"),
    )

    assert after_signal == len(records)
    assert (
        db_session.execute(
            select(func.count()).select_from(ResearchPaper)
        ).scalar_one()
        == after_signal
    )


def test_signal_and_investigation_matches_coexist(
    db_session: Session,
    investigation: Investigation,
    opportunity_signal: Signal,
    records: list[dict[str, Any]],
) -> None:
    research(
        db_session,
        research_subject_from_signal(opportunity_signal),
        records,
        ScoringMatcher(80.0, "signal verdict"),
    )
    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(90.0, "investigation verdict"),
    )

    opportunity_matches = db_session.execute(
        select(func.count()).select_from(OpportunityResearchMatch)
    ).scalar_one()
    investigation_matches = db_session.execute(
        select(func.count()).select_from(InvestigationResearchMatch)
    ).scalar_one()

    assert opportunity_matches > 0
    assert investigation_matches > 0


def test_a_verdict_for_one_subject_never_overwrites_the_other(
    db_session: Session,
    investigation: Investigation,
    opportunity_signal: Signal,
    records: list[dict[str, Any]],
) -> None:
    """THE CORE ISOLATION PROPERTY.

    Two different problem statements produced two different judgements
    about the same paper. Both remain readable, at their own scores, with
    their own reasons.
    """
    research(
        db_session,
        research_subject_from_signal(opportunity_signal),
        records,
        ScoringMatcher(80.0, "signal verdict"),
    )
    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(90.0, "investigation verdict"),
    )

    signal_scores = set(
        db_session.execute(select(OpportunityResearchMatch.relevance_score)).scalars()
    )
    investigation_scores = set(
        db_session.execute(
            select(InvestigationResearchMatch.relevance_score)
        ).scalars()
    )
    signal_reasons = set(
        db_session.execute(select(OpportunityResearchMatch.match_reason)).scalars()
    )

    assert signal_scores == {80.0}
    assert investigation_scores == {90.0}
    assert signal_reasons == {"signal verdict"}


def test_re_running_one_subject_leaves_the_other_untouched(
    db_session: Session,
    investigation: Investigation,
    opportunity_signal: Signal,
    records: list[dict[str, Any]],
) -> None:
    research(
        db_session,
        research_subject_from_signal(opportunity_signal),
        records,
        ScoringMatcher(80.0, "signal verdict"),
    )
    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(90.0, "investigation verdict"),
    )

    # The investigation is researched again with a different judgement.
    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(72.0, "revised investigation verdict"),
    )

    assert set(
        db_session.execute(select(OpportunityResearchMatch.relevance_score)).scalars()
    ) == {80.0}
    assert set(
        db_session.execute(
            select(InvestigationResearchMatch.relevance_score)
        ).scalars()
    ) == {72.0}


def test_each_subject_reads_back_only_its_own_research(
    db_session: Session,
    investigation: Investigation,
    opportunity_signal: Signal,
    records: list[dict[str, Any]],
) -> None:
    research(
        db_session,
        research_subject_from_signal(opportunity_signal),
        records,
        ScoringMatcher(80.0, "signal verdict"),
    )
    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(90.0, "investigation verdict"),
    )

    signal_view = get_subject_research_intelligence(
        db_session,
        subject_id=opportunity_signal.id,
        origin=ResearchSubjectOrigin.SIGNAL,
    )
    investigation_view = get_subject_research_intelligence(
        db_session,
        subject_id=investigation.id,
        origin=ResearchSubjectOrigin.INVESTIGATION,
    )

    assert signal_view.average_relevance_score == 80.0
    assert investigation_view.average_relevance_score == 90.0
    assert signal_view.subject_id == opportunity_signal.id
    assert signal_view.origin is ResearchSubjectOrigin.SIGNAL
    assert investigation_view.subject_id == investigation.id
    assert investigation_view.origin is ResearchSubjectOrigin.INVESTIGATION


def test_searches_are_attributed_to_one_subject_each(
    db_session: Session,
    investigation: Investigation,
    opportunity_signal: Signal,
    records: list[dict[str, Any]],
) -> None:
    """No search row ever claims both subjects."""
    research(
        db_session,
        research_subject_from_signal(opportunity_signal),
        records,
        ScoringMatcher(80.0, "signal verdict"),
    )
    research(
        db_session,
        research_subject_from_investigation(investigation),
        records,
        ScoringMatcher(90.0, "investigation verdict"),
    )

    runs = list(db_session.execute(select(ResearchSearchRun)).scalars())

    assert runs
    assert all(
        (r.signal_id is None) != (r.investigation_id is None) for r in runs
    ), "every search belongs to exactly one subject"


# -- one engine, two subjects ----------------------------------------------


def test_the_same_generator_plans_for_both_kinds_of_subject(
    db_session: Session, investigation: Investigation, opportunity_signal: Signal
) -> None:
    """No `generate_for_signal` / `generate_for_investigation` pair exists."""
    generator = ConceptQueryGenerator()

    signal_plan = generator.generate(research_subject_from_signal(opportunity_signal))
    investigation_plan = generator.generate(
        research_subject_from_investigation(investigation)
    )

    assert len(signal_plan.queries) == len(investigation_plan.queries) == 3
    assert signal_plan.subject_id == opportunity_signal.id
    assert investigation_plan.subject_id == investigation.id
