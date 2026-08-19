"""OpenAI as the semantic judge of pain <-> research relevance.

THE ADAPTER. `app.research_intelligence` owns the port (SemanticMatcher)
and the verdict contract; this module owns the provider. The dependency
points inward, so the research core stays free of any SDK and every stage
except this one is testable with no network.

What makes this different from the lexical development matcher it
replaces: that one counts shared words and therefore cannot see that
"unorganized tempo drivers with no fare transparency" and "dynamic
pricing in two-sided freight markets" are the same problem. This one is
asked to judge applicability, and is told explicitly that shared
vocabulary and a shared industry are not it.

Raw provider output never leaves this file. Everything returned to the
caller is a ResearchMatchVerdict, whose own validators clamp the scores,
normalize the concepts and refuse a blank reason -- so a model that
answers 150, or "", or the same concept three times, still cannot put a
malformed row in the database.
"""

import json
import logging
from typing import Any

import openai
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db.models import ResearchPaper
from app.integrations.openai.errors import (
    SemanticJudgeResponseError,
    SemanticJudgeTransportError,
    SemanticJudgeUnavailableError,
)
from app.research_intelligence.matching import ResearchMatchVerdict
from app.research_intelligence.schemas import MarketContext, ResearchQueryPlan

logger = logging.getLogger(__name__)

# Generous because gpt-5-mini is a reasoning model: reasoning tokens are
# billed as completion tokens and count against this ceiling, so a tight
# limit truncates the answer rather than the thinking. The visible answer
# itself is a few hundred tokens.
MAX_COMPLETION_TOKENS = 4000

# How much of an abstract is sent. Real arXiv abstracts topped out near
# 1,940 characters in the validated corpus, so this truncates nothing in
# practice -- it is a guard against a pathological record, not a budget.
MAX_ABSTRACT_CHARS = 6000

# The structured answer.
#
# No `minimum`/`maximum` keywords: OpenAI's strict structured outputs do
# not support them, and they would be the wrong place for the check
# anyway. ResearchMatchVerdict is the single validation authority --
# it clamps to 0-100, normalizes concepts and refuses a blank reason --
# so a score outside the band is corrected in one place rather than
# rejected by the provider and lost.
#
# Strict mode requires every property in `required`; optionality is
# expressed as a nullable union, which is how technical_readiness_score
# stays genuinely "not assessed" rather than absent.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevance_score": {
            "type": "integer",
            "description": (
                "0-100. How much this research materially helps solve, "
                "enable, improve or technically de-risk THIS market problem."
            ),
        },
        "matched_concepts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The technical concepts that actually connect the paper to "
                "the problem. Empty if nothing genuinely connects them."
            ),
        },
        "match_reason": {
            "type": "string",
            "description": (
                "One or two sentences on why this paper does or does not "
                "help THIS specific market problem. Never a summary of the "
                "paper on its own."
            ),
        },
        "technical_readiness_score": {
            "type": ["integer", "null"],
            "description": (
                "0-100 for how close the work is to being buildable on. "
                "NULL when the abstract does not support a judgement -- "
                "never a guess."
            ),
        },
    },
    "required": [
        "relevance_score",
        "matched_concepts",
        "match_reason",
        "technical_readiness_score",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You judge whether a research paper materially helps solve a real market problem.

The question is always: does this research help solve, enable, improve or \
technically de-risk THIS specific problem?

It is NOT "does the paper use the same words" and NOT "is the paper in the same \
industry". A paper full of matching vocabulary that solves a different problem is \
a LOW score. A paper that never uses the market's wording but supplies a method \
that would directly address the problem is a HIGH score.

Weigh five things, then give ONE score:

1. Problem alignment - does the paper address the underlying challenge, or just \
something adjacent to it?
2. Technical applicability - could its method, model or result plausibly \
contribute to a solution?
3. Specificity - is the connection direct, or merely "same sector"?
4. Research usefulness - is there a real technique or result here, or only \
topical overlap and survey material?
5. Technical readiness - is there enough evidence to say how buildable it is?

Score bands for relevance_score:
  90-100  very strong, direct applicability
  80-89   strong; clearly useful technical work
  70-79   meaningful supporting relevance
  50-69   related but indirect
  25-49   weak topical connection
  0-24    essentially irrelevant

Use the full range. Most retrieved papers are not strong matches, and saying so \
is the useful answer.

matched_concepts: the specific technical concepts that connect the two, in the \
paper's own terms. Return an empty list when nothing genuinely connects them - do \
not pad it with the market's vocabulary.

match_reason: one or two sentences explaining applicability to THIS market \
problem. Never a standalone summary of the paper.

technical_readiness_score: 0-100 for how close this is to being built on, or null \
when the abstract does not support that judgement. Null is the correct answer when \
you cannot tell; do not guess a number.
"""


def judge_prompt(
    context: MarketContext, plan: ResearchQueryPlan, paper: ResearchPaper
) -> str:
    """The one message. Market problem first, paper second, so the paper is
    read in light of the problem rather than summarized on its own."""
    categories = ", ".join(
        str(category.get("label") or category.get("code") or "")
        for category in (paper.categories or [])
    )
    return f"""\
MARKET PROBLEM
{context.problem}

DESCRIPTION
{context.description}

INDUSTRY
{context.industry or "(not stated)"}

RESEARCH CONCEPTS BEING SEARCHED FOR
{", ".join(plan.concepts) or "(none)"}

SEARCH QUERIES USED
{", ".join(plan.queries)}

---

PAPER TITLE
{paper.title}

PAPER CATEGORIES
{categories or "(none)"}

PAPER ABSTRACT
{paper.abstract[:MAX_ABSTRACT_CHARS]}
"""


class OpenAISemanticMatcher:
    """Semantic pain <-> research judgement, one paper per call.

    Satisfies app.research_intelligence.matching.SemanticMatcher.

    EVERY FAILURE DECLINES RATHER THAN SCORES. A timeout, a 5xx, a
    malformed answer and a refusal all return None, which the
    orchestration treats as "no opinion": nothing is written, nothing is
    deleted, and the verdicts already earned by other papers are
    untouched. Returning 0 instead would record "this research is
    irrelevant" on the strength of a network error.

    Failures are counted on the instance and logged, so a run where the
    provider was broken is distinguishable from one where the papers were
    genuinely poor -- which the protocol's None alone cannot express.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: openai.OpenAI | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_completion_tokens: int = MAX_COMPLETION_TOKENS,
    ) -> None:
        resolved = settings or get_settings()
        self.model = model or resolved.OPENAI_MODEL
        self.reasoning_effort = reasoning_effort or resolved.OPENAI_REASONING_EFFORT
        self.max_completion_tokens = max_completion_tokens
        # Counted, not raised: see the class docstring.
        self.transport_failures: list[str] = []
        self.response_failures: list[str] = []
        self.judged = 0
        # Accumulated so a pilot can report real token usage and cost.
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0

        if client is not None:
            self._client = client
            return
        if not resolved.OPENAI_API_KEY:
            raise SemanticJudgeUnavailableError(
                "OPENAI_API_KEY is not configured; the semantic matcher "
                "cannot be constructed. Use ConceptOverlapMatcher for a "
                "development run, or set the key."
            )
        self._client = openai.OpenAI(api_key=resolved.OPENAI_API_KEY)

    @property
    def failures(self) -> int:
        return len(self.transport_failures) + len(self.response_failures)

    def judge(
        self,
        *,
        context: MarketContext,
        plan: ResearchQueryPlan,
        paper: ResearchPaper,
    ) -> ResearchMatchVerdict | None:
        try:
            payload = self._request(context, plan, paper)
        except SemanticJudgeTransportError as exc:
            self.transport_failures.append(paper.arxiv_id)
            logger.warning(
                "semantic_judge_transport_failed",
                extra={"arxiv_id": paper.arxiv_id, "reason": str(exc)},
            )
            return None
        except SemanticJudgeResponseError as exc:
            self.response_failures.append(paper.arxiv_id)
            logger.warning(
                "semantic_judge_response_unusable",
                extra={"arxiv_id": paper.arxiv_id, "reason": str(exc)},
            )
            return None

        try:
            verdict = ResearchMatchVerdict(
                relevance_score=payload.get("relevance_score"),
                matched_concepts=payload.get("matched_concepts"),
                match_reason=payload.get("match_reason"),
                technical_readiness_score=payload.get("technical_readiness_score"),
            )
        except ValidationError as exc:
            # The verdict's own validators refused it -- a missing or
            # non-numeric score, a blank reason. Still a MATCHER failure,
            # never a zero: a judge that malfunctioned has said nothing
            # about the research.
            self.response_failures.append(paper.arxiv_id)
            logger.warning(
                "semantic_judge_verdict_rejected",
                extra={"arxiv_id": paper.arxiv_id, "reason": str(exc)},
            )
            return None

        self.judged += 1
        return verdict

    def _request(
        self, context: MarketContext, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> dict[str, Any]:
        """One structured judgement. Raw provider output stops here."""
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                # A reasoning model: `max_tokens` is rejected, reasoning
                # tokens count toward this ceiling, and a non-default
                # `temperature` is refused -- so neither is sent.
                max_completion_tokens=self.max_completion_tokens,
                reasoning_effort=self.reasoning_effort,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "research_match_verdict",
                        "strict": True,
                        "schema": VERDICT_SCHEMA,
                    },
                },
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": judge_prompt(context, plan, paper),
                    },
                ],
            )
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
        ) as exc:
            raise SemanticJudgeTransportError(f"{type(exc).__name__}") from exc
        except openai.APIStatusError as exc:
            # 5xx and 429 are transport; a 400 means this request was
            # wrong and will stay wrong, but either way one paper is
            # declined rather than the run aborted.
            raise SemanticJudgeTransportError(
                f"{type(exc).__name__}: HTTP {exc.status_code}"
            ) from exc

        self._record_usage(response)

        if not response.choices:
            raise SemanticJudgeResponseError("response carried no choices")
        choice = response.choices[0]

        if choice.finish_reason == "length":
            # The ceiling was hit before the answer was finished --
            # usually reasoning tokens. Not a verdict.
            raise SemanticJudgeResponseError(
                "response was truncated before a complete verdict"
            )
        if choice.finish_reason == "content_filter":
            raise SemanticJudgeResponseError("response was filtered")
        if getattr(choice.message, "refusal", None):
            raise SemanticJudgeResponseError("the model declined to judge this paper")

        text = (choice.message.content or "").strip()
        if not text:
            raise SemanticJudgeResponseError("response carried no content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SemanticJudgeResponseError("response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SemanticJudgeResponseError(
                f"response was a {type(payload).__name__}, expected an object"
            )
        return payload

    def _record_usage(self, response: Any) -> None:
        """Accumulate token usage so a pilot can report real cost."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None:
            self.reasoning_tokens += getattr(details, "reasoning_tokens", 0) or 0
