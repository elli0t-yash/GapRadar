"""The match table is many-to-many in both directions, and both are real.

Nothing writes OpportunityResearchMatch yet -- the matcher is a later
phase. These tests pin the storage contract it will have to satisfy, so
the shape is settled before anything depends on it.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import OpportunityResearchMatch, ResearchPaper, Signal
from app.research_intelligence.service import ingest_arxiv_search_results
from tests.research_intelligence.conftest import (
    VALIDATED_QUERY,
    arxiv_record_for,
    make_opportunity_signal,
)


def make_paper(db_session: Session, arxiv_id: str) -> ResearchPaper:
    """Persist one paper via a real search.

    The search needs a subject of its own: every ResearchSearchRun names
    exactly one, enforced by a CHECK. It is deliberately NOT one of the
    signals under test -- these tests count matches per opportunity, and
    borrowing a subject under test would entangle the fixture with the
    assertion.
    """
    probe = make_opportunity_signal(db_session, title=f"probe for {arxiv_id}")
    result = ingest_arxiv_search_results(
        db_session,
        query=VALIDATED_QUERY,
        records=[arxiv_record_for(arxiv_id)],
        signal_id=probe.id,
    )
    paper = db_session.get(ResearchPaper, result.research_paper_ids[0])
    assert paper is not None
    return paper


def match(
    db_session: Session,
    signal: Signal,
    paper: ResearchPaper,
    *,
    relevance_score: float = 0.82,
    commit: bool = True,
    **extra: Any,
) -> OpportunityResearchMatch:
    row = OpportunityResearchMatch(
        signal_id=signal.id,
        research_paper_id=paper.id,
        relevance_score=relevance_score,
        matched_concepts=extra.pop("matched_concepts", ["dynamic routing"]),
        **extra,
    )
    db_session.add(row)
    if commit:
        db_session.commit()
        db_session.refresh(row)
    return row


def test_a_match_records_the_full_verdict(
    db_session: Session, opportunity_signal: Signal
) -> None:
    paper = make_paper(db_session, "2608.13083")

    row = match(
        db_session,
        opportunity_signal,
        paper,
        relevance_score=0.91,
        technical_readiness_score=0.4,
        matched_concepts=["dynamic routing", "fleet dispatch"],
        match_reason="Both concern on-demand vehicle assignment under time windows.",
    )

    assert row.signal_id == opportunity_signal.id
    assert row.research_paper_id == paper.id
    assert row.relevance_score == 0.91
    assert row.technical_readiness_score == 0.4
    assert row.matched_concepts == ["dynamic routing", "fleet dispatch"]
    assert row.match_reason is not None
    assert row.created_at is not None


def test_many_opportunities_can_match_one_paper(db_session: Session) -> None:
    """One survey paper can serve several distinct market pains."""
    paper = make_paper(db_session, "2608.13083")
    signals = [
        make_opportunity_signal(db_session, title=f"Opportunity {index}")
        for index in range(3)
    ]

    for index, signal in enumerate(signals):
        match(db_session, signal, paper, relevance_score=0.5 + index / 10)

    rows = list(
        db_session.execute(
            select(OpportunityResearchMatch).where(
                OpportunityResearchMatch.research_paper_id == paper.id
            )
        ).scalars()
    )
    assert len(rows) == 3
    assert {row.signal_id for row in rows} == {signal.id for signal in signals}


def test_one_opportunity_can_match_many_papers(
    db_session: Session, opportunity_signal: Signal
) -> None:
    papers = [
        make_paper(db_session, arxiv_id)
        for arxiv_id in ("2608.13083", "2607.22582", "2607.10173")
    ]

    for index, paper in enumerate(papers):
        match(db_session, opportunity_signal, paper, relevance_score=0.9 - index / 10)

    rows = list(
        db_session.execute(
            select(OpportunityResearchMatch)
            .where(OpportunityResearchMatch.signal_id == opportunity_signal.id)
            .order_by(OpportunityResearchMatch.relevance_score.desc())
        ).scalars()
    )
    assert [row.research_paper_id for row in rows] == [paper.id for paper in papers]
    assert rows[0].relevance_score == pytest.approx(0.9)


def test_the_same_pair_cannot_be_claimed_twice(
    db_session: Session, opportunity_signal: Signal
) -> None:
    """One verdict per (opportunity, paper): a rerun updates, never stacks."""
    paper = make_paper(db_session, "2608.13083")
    match(db_session, opportunity_signal, paper)

    with pytest.raises(IntegrityError):
        match(db_session, opportunity_signal, paper, relevance_score=0.99)

    db_session.rollback()
    assert (
        db_session.execute(
            select(func.count()).select_from(OpportunityResearchMatch)
        ).scalar_one()
        == 1
    )


def test_a_match_without_a_relevance_score_is_refused(
    db_session: Session, opportunity_signal: Signal
) -> None:
    """A match with no score is an assertion with no evidence."""
    paper = make_paper(db_session, "2608.13083")

    db_session.add(
        OpportunityResearchMatch(
            signal_id=opportunity_signal.id,
            research_paper_id=paper.id,
            matched_concepts=[],
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_technical_readiness_and_reason_are_optional(
    db_session: Session, opportunity_signal: Signal
) -> None:
    """Null means "not assessed", never "not ready" and never "no reason"."""
    paper = make_paper(db_session, "2608.13083")

    row = match(db_session, opportunity_signal, paper, matched_concepts=[])

    assert row.technical_readiness_score is None
    assert row.match_reason is None
    assert row.matched_concepts == []


def test_a_match_is_anchored_to_a_real_signal_and_a_real_paper() -> None:
    """Both sides are foreign keys, asserted structurally.

    Not by inserting a dangling id: SQLite -- which this suite uses --
    does not enforce foreign keys unless PRAGMA foreign_keys=ON is set,
    and the shared conftest does not set it. Checking the mapped
    constraint proves the schema intent in a way that does not depend on
    which backend happens to be running, and the PostgreSQL migration
    creates the same two constraints.
    """
    table = OpportunityResearchMatch.__table__
    targets = {
        column.name: {fk.target_fullname for fk in column.foreign_keys}
        for column in table.columns
        if column.foreign_keys
    }

    assert targets["signal_id"] == {"signals.id"}
    assert targets["research_paper_id"] == {"research_papers.id"}
    assert not table.c.signal_id.nullable
    assert not table.c.research_paper_id.nullable
