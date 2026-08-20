"""LLM fallback for research queries the deterministic generator cannot form.

WHY THIS EXISTS. `ConceptQueryGenerator` maps problem wording onto a
curated research lexicon. When a problem uses none of that vocabulary, it
falls back to the industry name and produces "{industry} systems" --
plans an audit showed retrieve nothing and cost three provider jobs to
learn it. The quality gate rejects those, which is correct, and which
left a large share of legitimate opportunities unable to use Research
Analyse at all.

This is the second stage: when the deterministic plan is REJECTED, ask a
model to name the research concepts the problem is really about. It runs
only on rejection, so the deterministic path stays free, deterministic
and unchanged for everything it already handles.

THE GATE STILL DECIDES. Nothing here is trusted: output is validated by
the same `validate_plan` that rejected the deterministic attempt, plus
the extra checks below for shapes only a model produces. A provider call
is made ONLY after the fallback plan passes. A model that returns
marketing copy buys exactly zero Bright Data jobs.
"""

import json
import logging
import re
from typing import Any

import openai

from app.config import Settings, get_settings
from app.integrations.openai.errors import SemanticJudgeUnavailableError
from app.research_intelligence.query_generation import (
    ResearchQueryGenerationError,
    ResearchQueryProviderError,
)
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject

logger = logging.getLogger(__name__)

# Reasoning tokens count toward this, and the answer itself is tiny.
MAX_COMPLETION_TOKENS = 2000

QUERY_COUNT = 3

# An arXiv full-text search degrades badly on long strings: a sentence
# matches nothing. Two to six words is the shape the collector was
# validated against.
MIN_QUERY_WORDS = 2
MAX_QUERY_WORDS = 6

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The technical research areas this problem belongs to, in "
                "the vocabulary a paper would use."
            ),
        },
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                f"Exactly {QUERY_COUNT} short arXiv search queries, "
                f"{MIN_QUERY_WORDS}-{MAX_QUERY_WORDS} words each."
            ),
        },
    },
    "required": ["concepts", "queries"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You turn a real-world market problem into arXiv search queries.

The deterministic generator already failed on this problem: its wording \
matched no known research vocabulary. Your job is to name what the \
problem is ACTUALLY about in the language researchers use.

Return exactly 3 queries. Each must be 2-6 words and read like a \
research topic, not a product pitch.

GOOD queries name a mechanism, method or phenomenon:
  "vehicle routing time windows"
  "differential privacy health records"
  "demand forecasting intermittent series"
  "speech recognition low resource languages"

BAD queries -- never produce these:
  "fintech systems"                (industry name plus a generic noun)
  "logistics optimization platform" (product language)
  "a system that helps workers find affordable transport" (a sentence)
  "AI-powered solution"             (marketing)
  "GPT-5 routing engine"            (a named technology the problem did \
not mention)

Rules:
- Never build a query out of the industry name alone. The industry is \
context, not the subject.
- No two queries may be near-duplicates of each other; each should open \
a different line of research.
- Use only what the problem itself supports. Do not invent named \
technologies, products or datasets.
- If the problem genuinely has no research angle, return an empty \
queries list rather than padding it. An honest refusal is cheap; three \
useless provider jobs are not.
"""


def plan_prompt(subject: ResearchSubject) -> str:
    """The problem, as the model sees it. Contains no credential.

    Reads `subject.problem`. It used to read `context.title`, which no
    contract on this side has ever defined -- so every real fallback call
    raised AttributeError before reaching the model. Nothing caught it
    because no test constructed this generator against a live context.
    """
    parts = [
        "MARKET PROBLEM",
        subject.problem,
    ]
    if subject.description:
        parts += ["", "DESCRIPTION", subject.description]
    if subject.industry:
        parts += [
            "",
            "INDUSTRY (context only -- never the subject of a query)",
            subject.industry,
        ]
    return "\n".join(parts)


class LlmQueryPlanError(ResearchQueryGenerationError):
    """The fallback answered, and its answer was unusable.

    Subclasses the PORT's error rather than defining a parallel one, so
    the research core catches a contract it owns and never imports
    anything from app.integrations. A model that returned marketing copy
    and a deterministic generator that ran out of vocabulary are the same
    fact to a caller: this input yields no good queries.
    """


class OpenAIResearchQueryGenerator:
    """Generates a research query plan when the deterministic one is rejected.

    Satisfies app.research_intelligence.query_generation.
    ResearchQueryGenerator, so orchestration cannot tell it apart from the
    deterministic generator and no provider name leaks into the research
    core.

    Raises rather than returning a poor plan. The caller is deciding
    whether to spend three Bright Data jobs, and a quietly-degraded plan
    is the one outcome that costs money and returns nothing.
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
        self.prompt_tokens = 0
        self.completion_tokens = 0

        if client is not None:
            self._client = client
            return
        if not resolved.OPENAI_API_KEY:
            raise SemanticJudgeUnavailableError(
                "OPENAI_API_KEY is not configured; the research query "
                "fallback cannot be constructed."
            )
        self._client = openai.OpenAI(api_key=resolved.OPENAI_API_KEY)

    def generate(self, subject: ResearchSubject) -> ResearchQueryPlan:
        """One plan for one problem.

        Raises LlmQueryPlanError when the answer is unusable, and
        SemanticJudgeTransportError when the provider could not be
        reached -- the caller maps those to different outcome reasons
        because only one of them is worth retrying.
        """
        payload = self._request(subject)
        queries = _clean_queries(payload.get("queries"), subject)
        concepts = _clean_concepts(payload.get("concepts"))

        if len(queries) != QUERY_COUNT:
            raise LlmQueryPlanError(
                f"expected {QUERY_COUNT} usable queries, got {len(queries)}"
            )
        if not concepts:
            raise LlmQueryPlanError("no usable research concepts were returned")

        logger.info(
            "[research-enrichment] llm query fallback produced queries=%r",
            queries,
        )
        return ResearchQueryPlan(
            subject_id=subject.subject_id, queries=queries, concepts=concepts
        )

    def _request(self, subject: ResearchSubject) -> dict[str, Any]:
        """One structured plan. Raw provider output stops here."""
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_completion_tokens=self.max_completion_tokens,
                reasoning_effort=self.reasoning_effort,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "research_query_plan",
                        "strict": True,
                        "schema": PLAN_SCHEMA,
                    },
                },
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": plan_prompt(subject)},
                ],
            )
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
        ) as exc:
            raise ResearchQueryProviderError(f"{type(exc).__name__}") from exc
        except openai.APIStatusError as exc:
            raise ResearchQueryProviderError(
                f"{type(exc).__name__}: HTTP {exc.status_code}"
            ) from exc

        self._record_usage(response)

        if not response.choices:
            raise ResearchQueryProviderError("response carried no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ResearchQueryProviderError(
                "response hit the completion ceiling before finishing"
            )
        content = choice.message.content
        if not content:
            raise ResearchQueryProviderError("response carried no content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ResearchQueryProviderError("response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ResearchQueryProviderError("response was not a JSON object")
        return payload

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0


# -- validating what a model returned ---------------------------------------

# Words that make a query a pitch rather than a research topic. Checked as
# whole words so "platform" is rejected while "platformer" would not be.
_MARKETING_TERMS = frozenset(
    {
        "ai-powered",
        "app",
        "b2b",
        "b2c",
        "best",
        "cutting-edge",
        "customers",
        "enterprise",
        "innovative",
        "market",
        "monetization",
        "next-generation",
        "platform",
        "product",
        "revenue",
        "saas",
        "scalable",
        "seamless",
        "solution",
        "startup",
        "state-of-the-art",
        "users",
    }
)

# The same fallback shape the deterministic generator produces. Rejected
# here too, so the model cannot reintroduce what the gate exists to stop.
_GENERIC_TAIL_WORDS = frozenset({"systems", "system", "technology", "tech"})


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9-]+", value.lower())


def _clean_queries(raw: Any, subject: ResearchSubject) -> list[str]:
    """Keep only queries worth spending a provider job on.

    Every rejection here is a query that would otherwise have cost a real
    Bright Data run, so the checks are deliberately strict and each one
    corresponds to a shape actually observed or explicitly forbidden.
    """
    if not isinstance(raw, list):
        return []

    industry = _normalize(subject.industry or "")
    industry_tokens = set(_tokens(industry)) if industry else set()

    kept: list[str] = []
    seen_signatures: set[frozenset[str]] = set()

    for item in raw:
        if not isinstance(item, str):
            continue
        query = _normalize(item)
        if not query:
            continue

        words = _tokens(query)
        if not (MIN_QUERY_WORDS <= len(words) <= MAX_QUERY_WORDS):
            # A single word is not a search; a sentence is not either.
            continue
        if len(words) != len(set(words)):
            # "demand forecasting demand forecasting" -- malformed, and
            # already the deterministic gate's rule.
            continue
        if _MARKETING_TERMS.intersection(words):
            continue
        if industry_tokens and set(words) <= industry_tokens | _GENERIC_TAIL_WORDS:
            # "fintech systems" -- the exact fallback this whole stage
            # exists to replace.
            continue
        if words and words[-1] in _GENERIC_TAIL_WORDS and len(words) <= 2:
            continue

        # Near-duplicate detection on the word SET, so "urban freight
        # routing" and "routing urban freight" count as one idea rather
        # than two lines of research.
        signature = frozenset(words)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        kept.append(query)

    return kept


def _clean_concepts(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    kept: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        concept = _normalize(item)
        if concept and concept not in kept:
            kept.append(concept)
    return kept


__all__ = [
    "LlmQueryPlanError",
    "OpenAIResearchQueryGenerator",
    "plan_prompt",
]
