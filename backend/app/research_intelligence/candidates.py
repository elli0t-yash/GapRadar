"""Cheap, transparent narrowing before anything expensive judges relevance.

Three queries at fifteen results each is up to forty-five papers, and
most of them are wrong. Sending all of them to a semantic matcher costs
real money per opportunity and buries the good ones in noise.

So this ranks candidates by lexical overlap and keeps the top handful.
IT IS NOT SEMANTIC MATCHING AND DOES NOT CLAIM TO BE. It cannot tell that
"fleet dispatch" and "vehicle assignment" are the same idea; it only
counts words the two texts literally share. Its job is to be fast,
deterministic and explainable enough that a human can see why a paper
survived -- and to hand a small, plausible set to something that does
understand meaning.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import ResearchPaper
from app.research_intelligence.query_generation import tokenize
from app.research_intelligence.schemas import MarketContext, ResearchQueryPlan

# How many papers reach the semantic stage. Sized so three 15-result
# searches (~45 papers, fewer after dedupe) are cut to roughly a quarter:
# small enough to judge affordably, large enough that a good paper ranked
# eighth by lexical overlap alone still gets its chance.
DEFAULT_CANDIDATE_LIMIT = 12

# A title match is worth roughly twice an abstract match: a paper whose
# TITLE shares the problem's vocabulary is far more likely to be about
# that problem than one that mentions it once in passing.
TITLE_WEIGHT = 0.65
ABSTRACT_WEIGHT = 0.35


class RankedCandidate(BaseModel):
    """One paper's pre-filter verdict, with the reason attached.

    `matched_tokens` is what makes this auditable: it is the literal set
    of words that earned the score, so a surprising ranking can be read
    rather than guessed at.
    """

    model_config = ConfigDict(frozen=True)

    research_paper_id: uuid.UUID
    arxiv_id: str
    # 0-100, matching every other research-intelligence score.
    score: float
    matched_tokens: list[str] = Field(default_factory=list)


def context_tokens(context: MarketContext, plan: ResearchQueryPlan) -> set[str]:
    """The vocabulary this opportunity is searching with.

    Drawn from the problem, the industry, the generated concepts and the
    queries themselves -- the description is deliberately excluded. It is
    several hundred words of prose whose incidental vocabulary ("city",
    "apps", "people") matches almost any paper and would flatten the
    ranking toward noise.
    """
    tokens: set[str] = set()
    tokens.update(tokenize(context.problem))
    if context.industry:
        tokens.update(tokenize(context.industry))
    for concept in plan.concepts:
        tokens.update(tokenize(concept))
    for query in plan.queries:
        tokens.update(tokenize(query))
    return tokens


def score_paper(tokens: set[str], paper: ResearchPaper) -> tuple[float, list[str]]:
    """Score one paper 0-100 against the context vocabulary.

    Overlap is measured as the fraction of the CONTEXT's vocabulary the
    paper covers, not the fraction of the paper's -- otherwise a short
    title would outrank a thorough abstract purely for being short.

    Categories are deliberately not used. There is no principled mapping
    from a market industry to an arXiv subject code yet, and inventing
    one would silently bias retrieval toward whichever codes the guess
    happened to name.
    """
    if not tokens:
        return 0.0, []

    title_tokens = set(tokenize(paper.title))
    abstract_tokens = set(tokenize(paper.abstract))

    title_hits = tokens & title_tokens
    abstract_hits = tokens & abstract_tokens

    score = 100.0 * (
        TITLE_WEIGHT * len(title_hits) / len(tokens)
        + ABSTRACT_WEIGHT * len(abstract_hits) / len(tokens)
    )
    return round(score, 2), sorted(title_hits | abstract_hits)


def rank_candidates(
    context: MarketContext,
    plan: ResearchQueryPlan,
    papers: list[ResearchPaper],
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[RankedCandidate]:
    """Rank papers by lexical overlap and keep the top `limit`.

    Papers with no overlap at all are dropped rather than ranked last: a
    paper sharing not one word with the problem is not a weak candidate,
    it is a different subject, and carrying it forward only spends
    semantic budget to be told so.

    Ordering is (score descending, arxiv_id ascending). The arxiv_id
    tiebreak is what makes the result stable: without it, two papers on
    the same score would rank by whatever order the database happened to
    return, and the same input would produce different candidate sets.
    """
    tokens = context_tokens(context, plan)

    scored: list[RankedCandidate] = []
    for paper in papers:
        score, matched = score_paper(tokens, paper)
        if score <= 0.0:
            continue
        scored.append(
            RankedCandidate(
                research_paper_id=paper.id,
                arxiv_id=paper.arxiv_id,
                score=score,
                matched_tokens=matched,
            )
        )

    scored.sort(key=lambda candidate: (-candidate.score, candidate.arxiv_id))
    return scored[:limit]
