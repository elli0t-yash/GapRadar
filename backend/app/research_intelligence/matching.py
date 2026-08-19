"""Judging whether a paper actually addresses a market pain.

This is the stage that needs to understand meaning: that "unorganized
tempo drivers with no fare transparency" and "dynamic pricing in
two-sided freight markets" are the same problem described from two
sides. Lexical overlap cannot do that, which is why the pre-filter stops
where it does and this begins.

No LLM provider exists in this repository, and none is added here.
What exists instead is:

- SemanticMatcher, the protocol the real matcher will satisfy;
- ResearchMatchVerdict, which clamps and normalizes whatever a matcher
  returns, because an LLM will eventually return 150, an empty reason,
  or the same concept three times;
- ConceptOverlapMatcher, a deterministic stand-in that makes the whole
  pipeline runnable and testable today and is explicitly NOT semantic.
"""

import math
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import ResearchPaper
from app.research_intelligence.candidates import (
    DEFAULT_CANDIDATE_LIMIT,
    context_tokens,
    score_paper,
)
from app.research_intelligence.query_generation import tokenize
from app.research_intelligence.schemas import MarketContext, ResearchQueryPlan

# Every research-intelligence score is 0-100, matching opportunity_score
# on the market side so nothing has to remember which scale it is on.
MIN_SCORE = 0.0
MAX_SCORE = 100.0

# A paper below this is not shown. Retrieval has already narrowed the set,
# so the remaining job is to prefer a small explainable list over a long
# plausible one: a user who opens "research behind this problem" and finds
# three tangential papers stops trusting the feature.
DEFAULT_RELEVANCE_THRESHOLD = 70.0


class ResearchMatchPolicy(BaseModel):
    """Tunable knobs for one enrichment. Not contractual.

    Named and passed rather than referenced as constants throughout, so a
    caller can lower the threshold for an exploratory run without editing
    module-level state that every other caller shares.
    """

    model_config = ConfigDict(frozen=True)

    relevance_threshold: float = Field(
        default=DEFAULT_RELEVANCE_THRESHOLD, ge=MIN_SCORE, le=MAX_SCORE
    )
    candidate_limit: int = Field(default=DEFAULT_CANDIDATE_LIMIT, ge=1, le=100)


DEFAULT_MATCH_POLICY = ResearchMatchPolicy()


def clamp_score(value: Any) -> float | None:
    """Force a score into 0-100, or None if it is not a number at all.

    THE APPLICATION-LAYER GUARANTEE. A matcher -- especially an LLM one --
    will return 150, -3, "87", or None. Clamping here means every score
    that reaches the database is in range no matter what produced it, and
    a nonsense value degrades to a boundary rather than corrupting a
    ranking. A non-numeric value becomes None, never 0: "could not say"
    and "said zero" are different answers.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(MIN_SCORE, min(MAX_SCORE, number))


class ResearchMatchVerdict(BaseModel):
    """One matcher's judgement about one (opportunity, paper) pair.

    Validation runs on construction, so an invalid verdict cannot exist:
    scores are clamped into range, concepts are stripped/deduped, and a
    blank reason is refused. A future LLM adapter therefore needs no
    validation of its own -- it constructs one of these and the rules
    apply.
    """

    model_config = ConfigDict(frozen=True)

    # Required. A match with no relevance score is an assertion with no
    # evidence, which is also why the column is NOT NULL.
    relevance_score: float = Field(ge=MIN_SCORE, le=MAX_SCORE)
    matched_concepts: list[str] = Field(default_factory=list)
    # Must explain applicability to THIS market problem, not summarize
    # the paper.
    match_reason: str
    # None means "not assessed", never "not ready". Left null rather than
    # guessed when the evidence does not support a judgement.
    technical_readiness_score: float | None = None

    @field_validator("relevance_score", "technical_readiness_score", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float | None:
        return clamp_score(value)

    @field_validator("matched_concepts", mode="before")
    @classmethod
    def _clean_concepts(cls, value: Any) -> list[str]:
        """Strip, drop blanks and non-strings, dedupe case-insensitively.

        Order is preserved: a matcher lists its strongest concept first,
        and reordering would discard that.
        """
        if not isinstance(value, list):
            return []
        concepts: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            concept = " ".join(item.split())
            if not concept or concept.lower() in seen:
                continue
            seen.add(concept.lower())
            concepts.append(concept)
        return concepts

    @field_validator("match_reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("match_reason must be a non-blank string")
        return " ".join(value.split())


@runtime_checkable
class ReportsJudgingFailures(Protocol):
    """A matcher that can say how often it FAILED, as opposed to declined.

    The SemanticMatcher protocol returns None for "no opinion", which
    deliberately covers both "this paper is unjudgeable" and "the
    provider broke". That conflation is correct for one paper and wrong
    for a run: a judge that never answered at all must not look like a
    judge that answered "no" every time.

    Optional on purpose. A deterministic matcher has no failures to
    report and does not implement this; callers check with isinstance and
    treat its absence as zero.
    """

    @property
    def failures(self) -> int: ...


class SemanticMatcher(Protocol):
    """Judges whether one paper addresses one market pain.

    Returning None means the matcher DECLINED to judge -- it could not
    reach an opinion, the provider errored, the response was unusable.
    That is different from judging the paper irrelevant, which is a
    verdict with a low score, and orchestration treats them differently:
    a declined judgement is not evidence of anything.

    The full context is passed rather than a pre-built prompt so the
    adapter owns its own prompting, and swapping models never touches
    orchestration.
    """

    def judge(
        self,
        *,
        context: MarketContext,
        plan: ResearchQueryPlan,
        paper: ResearchPaper,
    ) -> ResearchMatchVerdict | None: ...


class ConceptOverlapMatcher:
    """Deterministic stand-in. NOT a semantic matcher.

    It reuses the pre-filter's lexical overlap and dresses the result up
    as a verdict. It cannot recognise a paraphrase, an analogy, or a
    method applied to a different domain -- which is most of what this
    stage exists to catch.

    It is here for three honest reasons: the pipeline can be built and
    demonstrated end to end before an LLM is chosen; every other stage
    gets tested against a matcher whose output is predictable; and the
    real matcher has a baseline it must visibly beat.

    `technical_readiness_score` is always None. Nothing in a title-and-
    abstract word count is evidence about how buildable the research is,
    and returning a number anyway would be exactly the fabrication the
    null is there to prevent.

    NO SCORE SCALING. An earlier version multiplied the overlap by a
    `scale` factor so its scores would reach a threshold expressed on the
    0-100 band. The first real pilot showed what that actually did: raw
    overlaps of 33.54, 25.21, 18.33 and 17.08 all multiplied past 100 and
    clamped to exactly 100, so four visibly different papers reported an
    identical score and 12 of 12 candidates cleared the threshold. The
    scaling destroyed the only signal this matcher has.

    So the overlap is reported as measured. Real consequence, stated
    plainly: lexical scores land roughly in the 5-35 band and this matcher
    will almost never clear a threshold of 70 on its own. That is the
    honest answer -- shared vocabulary is weak evidence of relevance --
    and a caller that wants matches out of the development matcher should
    lower ResearchMatchPolicy.relevance_threshold deliberately rather
    than inflate the score.
    """

    def judge(
        self,
        *,
        context: MarketContext,
        plan: ResearchQueryPlan,
        paper: ResearchPaper,
    ) -> ResearchMatchVerdict | None:
        tokens = context_tokens(context, plan)
        score, matched = score_paper(tokens, paper)
        if score <= 0.0:
            return None

        concepts = _concepts_for(matched, plan)
        return ResearchMatchVerdict(
            # Reported as measured. The overlap score is ALREADY on 0-100
            # (it is a weighted coverage fraction times 100), so nothing
            # is transformed on the way out.
            relevance_score=score,
            matched_concepts=concepts,
            match_reason=(
                f"Lexical overlap only: the paper's title and abstract share "
                f"{len(matched)} term(s) with the problem "
                f"{context.problem!r}"
                + (f" -- {', '.join(concepts[:3])}." if concepts else ".")
            ),
            technical_readiness_score=None,
        )


def _concepts_for(matched_tokens: list[str], plan: ResearchQueryPlan) -> list[str]:
    """Report overlapping words as the plan's concepts where possible.

    A raw token like "routing" is less useful to a reader than the
    concept it came from ("vehicle routing"), so a plan concept
    containing a matched token is reported instead. Tokens belonging to
    no concept are reported as themselves rather than dropped.
    """
    hits = set(matched_tokens)
    concepts = [concept for concept in plan.concepts if hits & set(tokenize(concept))]
    covered = {token for concept in concepts for token in tokenize(concept)}
    return concepts + sorted(hits - covered)
