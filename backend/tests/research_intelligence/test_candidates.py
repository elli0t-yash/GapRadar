"""The cheap filter: relevant papers rise, unrelated ones are dropped.

It is lexical overlap, not understanding, and these tests pin both what
it catches and what it provably cannot.
"""

import uuid
from datetime import date

from app.db.models import ResearchPaper
from app.domain.enums import ResearchSource, ResearchSubjectOrigin
from app.research_intelligence.candidates import (
    DEFAULT_CANDIDATE_LIMIT,
    context_tokens,
    rank_candidates,
    score_paper,
)
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject

SIGNAL_ID = uuid.uuid4()

CONTEXT = ResearchSubject(
    subject_id=SIGNAL_ID,
    origin=ResearchSubjectOrigin.SIGNAL,
    problem="Why is booking cargo vehicles harder than passenger transport?",
    description="Long prose about furniture, apps, cities and people.",
    industry="Logistics",
)

PLAN = ResearchQueryPlan(
    subject_id=SIGNAL_ID,
    queries=[
        "on-demand allocation urban freight",
        "urban freight optimization",
        "vehicle routing demand forecasting",
    ],
    concepts=["on-demand allocation", "urban freight", "vehicle routing"],
    rationale="test",
)


def paper(
    arxiv_id: str, *, title: str, abstract: str = "An abstract."
) -> ResearchPaper:
    """An unpersisted paper -- the pre-filter never touches the database."""
    return ResearchPaper(
        id=uuid.uuid4(),
        arxiv_id=arxiv_id,
        source=ResearchSource.ARXIV,
        title=title,
        abstract=abstract,
        authors=["A. Researcher"],
        categories=[{"code": "math.OC", "label": "Optimization and Control"}],
        primary_category_code="math.OC",
        published_at=date(2026, 8, 13),
        paper_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


RELEVANT = paper(
    "2608.00001",
    title="Dynamic vehicle routing for urban freight allocation",
    abstract="We study on-demand freight vehicle routing in congested cities.",
)
TANGENTIAL = paper(
    "2608.00002",
    title="A survey of scheduling heuristics",
    abstract="Vehicle fleets are mentioned once among many applications.",
)
MODERATE = paper(
    "2608.00004",
    title="Freight routing under uncertainty",
    abstract="Routing methods for logistics networks.",
)
UNRELATED = paper(
    "2608.00003",
    title="Protein folding with quantum annealing",
    abstract="We fold proteins. Nothing here concerns markets or movement.",
)


# -- ranking ----------------------------------------------------------------


def test_a_relevant_paper_outranks_a_tangential_one() -> None:
    ranked = rank_candidates(CONTEXT, PLAN, [TANGENTIAL, RELEVANT])

    assert [candidate.arxiv_id for candidate in ranked] == [
        RELEVANT.arxiv_id,
        TANGENTIAL.arxiv_id,
    ]


def test_an_unrelated_paper_is_dropped_not_merely_ranked_last() -> None:
    """Zero overlap is a different subject, not a weak candidate."""
    ranked = rank_candidates(CONTEXT, PLAN, [RELEVANT, UNRELATED])

    assert [candidate.arxiv_id for candidate in ranked] == [RELEVANT.arxiv_id]


def test_a_title_match_outweighs_an_abstract_match() -> None:
    in_title = paper(
        "2608.00010", title="Urban freight vehicle routing", abstract="Nothing."
    )
    in_abstract = paper(
        "2608.00011",
        title="A general method",
        abstract="Urban freight vehicle routing is one application.",
    )

    ranked = rank_candidates(CONTEXT, PLAN, [in_abstract, in_title])

    assert ranked[0].arxiv_id == in_title.arxiv_id


def test_ranking_is_deterministic_regardless_of_input_order() -> None:
    papers = [RELEVANT, TANGENTIAL, paper("2608.00004", title="Freight routing basics")]

    first = [c.arxiv_id for c in rank_candidates(CONTEXT, PLAN, papers)]
    second = [
        c.arxiv_id for c in rank_candidates(CONTEXT, PLAN, list(reversed(papers)))
    ]

    assert first == second


def test_papers_on_an_equal_score_break_ties_by_arxiv_id() -> None:
    """Without a stable tiebreak the candidate set would drift per query."""
    twins = [
        paper("2608.00099", title="Urban freight routing"),
        paper("2608.00001", title="Urban freight routing"),
    ]

    ranked = rank_candidates(CONTEXT, PLAN, twins)

    assert [c.arxiv_id for c in ranked] == ["2608.00001", "2608.00099"]
    assert ranked[0].score == ranked[1].score


# -- the cap ----------------------------------------------------------------


def test_the_candidate_set_is_capped() -> None:
    """~45 search results must not all reach the expensive stage."""
    many = [
        paper(f"2608.{index:05d}", title="Urban freight vehicle routing study")
        for index in range(45)
    ]

    assert len(rank_candidates(CONTEXT, PLAN, many)) == DEFAULT_CANDIDATE_LIMIT
    # Raised 12 -> 18 on pilot evidence: the cap, not the zero-overlap
    # rule, was what removed 11 of 23 real papers.
    assert DEFAULT_CANDIDATE_LIMIT == 18


def test_the_cap_is_configurable() -> None:
    many = [paper(f"2608.{i:05d}", title="Urban freight routing") for i in range(20)]

    assert len(rank_candidates(CONTEXT, PLAN, many, limit=3)) == 3


def test_no_papers_yields_no_candidates() -> None:
    assert rank_candidates(CONTEXT, PLAN, []) == []


# -- explainability ---------------------------------------------------------


def test_a_candidate_reports_the_tokens_that_earned_its_score() -> None:
    ranked = rank_candidates(CONTEXT, PLAN, [RELEVANT])

    assert "freight" in ranked[0].matched_tokens
    assert "routing" in ranked[0].matched_tokens
    assert ranked[0].matched_tokens == sorted(ranked[0].matched_tokens)


def test_scores_are_on_the_zero_to_one_hundred_scale() -> None:
    ranked = rank_candidates(CONTEXT, PLAN, [RELEVANT, TANGENTIAL])

    assert all(0.0 < candidate.score <= 100.0 for candidate in ranked)


# -- what it draws on, and what it deliberately ignores ---------------------


def test_the_description_is_excluded_from_the_vocabulary() -> None:
    """Prose vocabulary matches almost anything and would flatten ranking."""
    tokens = context_tokens(CONTEXT, PLAN)

    assert "furniture" not in tokens
    assert "cities" not in tokens
    assert "freight" in tokens
    assert "logistics" in tokens


def test_the_pre_filter_cannot_recognise_a_paraphrase() -> None:
    """The honest limitation this stage hands off rather than solves.

    This paper is ABOUT the cargo-booking problem, described entirely in
    other words. Lexical overlap cannot see that, so it is dropped
    outright -- while a survey that merely says "vehicle fleets" once
    survives. Recovering the paraphrase is exactly what the semantic
    matcher is for, and this test exists so nobody mistakes the
    pre-filter for it.
    """
    paraphrase = paper(
        "2608.00050",
        title="Assignment of goods carriers to consumer requests",
        abstract="Matching haulage capacity to shippers in metropolitan areas.",
    )

    tokens = context_tokens(CONTEXT, PLAN)
    paraphrase_score, _ = score_paper(tokens, paraphrase)
    tangential_score, _ = score_paper(tokens, TANGENTIAL)

    assert paraphrase_score == 0.0
    assert tangential_score > paraphrase_score
    # Dropped entirely, despite being the more relevant paper.
    assert rank_candidates(CONTEXT, PLAN, [paraphrase, TANGENTIAL]) == [
        candidate for candidate in rank_candidates(CONTEXT, PLAN, [TANGENTIAL])
    ]
