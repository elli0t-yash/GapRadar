"""OpenAI as the judge of what a discovered web page means.

The semantic half of web discovery. Acquisition says a page exists and
what its title and snippet are; this decides whether that page is
evidence the problem is real, or a product competing for the same buyer.

WHY IT LIVES HERE. app.web_intelligence defines the ports
(DemandClassifier, CompetitorClassifier) and must never import a
provider -- that is what lets planning, normalization and orchestration
be tested with no network. This module is the adapter, so the dependency
points inward.

IT JUDGES A TITLE AND A SNIPPET, and its prompts say so. Discovery does
not open the page, so the model is told explicitly that it is reasoning
from a search result rather than from the article, and is instructed to
answer conservatively when the snippet does not support a judgement.
Pretending otherwise would produce confident verdicts about text nobody
read.
"""

import json
import logging
from typing import Any, ClassVar

import openai
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
)
from app.integrations.openai.errors import (
    SemanticJudgeResponseError,
    SemanticJudgeTransportError,
    SemanticJudgeUnavailableError,
)
from app.research_intelligence.schemas import ResearchSubject
from app.web_intelligence.classification import (
    CompetitorVerdict,
    DemandVerdict,
)
from app.web_intelligence.schemas import WebIntelligenceRecord

logger = logging.getLogger(__name__)

MAX_COMPLETION_TOKENS = 2000

DEMAND_SYSTEM_PROMPT = """\
You judge whether ONE web search result is evidence that a stated problem is \
really experienced by real people.

You are reading a SEARCH RESULT -- a title and a snippet -- not the page. \
Judge only what that text supports. When it does not support a judgement, say \
so with a lower relevance score and a neutral classification rather than \
guessing at what the page probably says.

classification:
- strong_support: the text describes people actually experiencing this \
problem, with specifics (frequency, cost, consequences).
- support: the text indicates this problem occurs, without specifics.
- neutral: the text is about this problem area but takes no position on \
whether it is experienced.
- contradicts: the text indicates the problem does not occur, is rare, or is \
already adequately solved.
- irrelevant: the text is not about this problem at all.

"irrelevant" and "neutral" are different: neutral is on-topic and \
non-committal, irrelevant is off-topic. A vendor's marketing page claiming the \
problem exists is weak evidence, not strong -- it is selling a solution.

relevance_score: 0-100 for how much this text bears on THIS problem.

reason: one or two sentences about applicability to THIS problem. Never a \
summary of the page.
"""

COMPETITOR_SYSTEM_PROMPT = """\
You judge how ONE web search result relates to a startup idea.

You are reading a SEARCH RESULT -- a title and a snippet -- not the page or \
the product. Judge only what that text supports.

classification:
- direct: solves the same problem for the same kind of buyer.
- adjacent: solves a neighbouring problem, or the same problem for a \
different buyer.
- substitute: not a product in this category, but what people use instead \
today (a spreadsheet, an agency, a manual process).
- irrelevant: not a solution to anything this idea is about.

name: the product or company as the TEXT names it. If the text does not name \
one clearly, use the result's title verbatim. Never invent a company name and \
never infer one from the domain.

relevance_score: 0-100 for how directly this competes with the idea.

reason: one or two sentences about the relationship to THIS idea.
"""

_DEMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [member.value for member in DemandEvidenceClassification],
        },
        "relevance_score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["classification", "relevance_score", "reason"],
    "additionalProperties": False,
}

_COMPETITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [member.value for member in CompetitorClassification],
        },
        "name": {"type": "string"},
        "relevance_score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["classification", "name", "relevance_score", "reason"],
    "additionalProperties": False,
}


def evidence_prompt(
    subject: ResearchSubject, record: WebIntelligenceRecord
) -> str:
    """The idea and the search result, in that order. No credential."""
    return f"""\
THE IDEA BEING INVESTIGATED
{subject.problem}

CONTEXT
{subject.description}

INDUSTRY
{subject.industry or "(not stated)"}

SEARCH RESULT (title and snippet only -- the page was NOT opened)
QUERY: {record.query}
TITLE: {record.title}
DOMAIN: {record.domain}
SNIPPET: {record.snippet or "(none)"}
"""


class _OpenAIWebClassifier:
    """Shared transport, failure accounting and response handling.

    `failures` counts times the JUDGE broke, not times it said
    "irrelevant". Orchestration needs the difference: a classifier that
    never answered must not look like one that rejected everything.
    """

    system_prompt: ClassVar[str] = ""
    schema_name: ClassVar[str] = ""
    schema: ClassVar[dict[str, Any]] = {}

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
        self.transport_failures: list[str] = []
        self.response_failures: list[str] = []
        self.judged = 0

        if client is not None:
            self._client = client
            return
        if not resolved.OPENAI_API_KEY:
            raise SemanticJudgeUnavailableError(
                "OPENAI_API_KEY is not configured; the web evidence "
                "classifier cannot be constructed."
            )
        self._client = openai.OpenAI(api_key=resolved.OPENAI_API_KEY)

    @property
    def failures(self) -> int:
        return len(self.transport_failures) + len(self.response_failures)

    def _judge_payload(
        self, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> dict[str, Any] | None:
        """One structured judgement, or None if the judge could not give one.

        Raw provider output stops here. Every failure path returns None
        and increments a counter rather than raising, because one
        unjudgeable page must not fail a phase that judged twenty others.
        """
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_completion_tokens=self.max_completion_tokens,
                reasoning_effort=self.reasoning_effort,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": self.schema_name,
                        "strict": True,
                        "schema": self.schema,
                    },
                },
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": evidence_prompt(subject, record),
                    },
                ],
            )
        except openai.OpenAIError as exc:
            self.transport_failures.append(record.url)
            logger.warning(
                "web_evidence_judge_transport_failed",
                extra={"domain": record.domain, "reason": str(exc)},
            )
            raise SemanticJudgeTransportError(str(exc)) from exc

        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice and choice.message else None
        if not content:
            self.response_failures.append(record.url)
            logger.warning(
                "web_evidence_judge_empty_response",
                extra={"domain": record.domain},
            )
            raise SemanticJudgeResponseError("the judge returned no content")

        try:
            payload = json.loads(content)
        except ValueError as exc:
            self.response_failures.append(record.url)
            raise SemanticJudgeResponseError(
                "the judge's response was not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            self.response_failures.append(record.url)
            raise SemanticJudgeResponseError(
                "the judge's response was not an object"
            )
        return payload


class OpenAIDemandClassifier(_OpenAIWebClassifier):
    """Judges whether one search result evidences the stated problem."""

    system_prompt: ClassVar[str] = DEMAND_SYSTEM_PROMPT
    schema_name: ClassVar[str] = "demand_evidence_verdict"
    schema: ClassVar[dict[str, Any]] = _DEMAND_SCHEMA

    def classify(
        self, *, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> DemandVerdict | None:
        try:
            payload = self._judge_payload(subject, record)
        except (SemanticJudgeTransportError, SemanticJudgeResponseError):
            return None
        if payload is None:  # pragma: no cover - defensive
            return None

        try:
            verdict = DemandVerdict(
                classification=payload.get("classification"),
                relevance_score=payload.get("relevance_score"),
                reason=payload.get("reason"),
            )
        except ValidationError as exc:
            # The verdict's own validators refused it -- an unknown
            # classification, a non-numeric score, a blank reason. Still a
            # JUDGE failure, never a default: a judge that malfunctioned
            # has said nothing about this page.
            self.response_failures.append(record.url)
            logger.warning(
                "web_evidence_verdict_rejected",
                extra={"domain": record.domain, "reason": str(exc)},
            )
            return None

        self.judged += 1
        return verdict


class OpenAICompetitorClassifier(_OpenAIWebClassifier):
    """Judges how one search result relates to the investigated idea."""

    system_prompt: ClassVar[str] = COMPETITOR_SYSTEM_PROMPT
    schema_name: ClassVar[str] = "competitor_verdict"
    schema: ClassVar[dict[str, Any]] = _COMPETITOR_SCHEMA

    def classify(
        self, *, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> CompetitorVerdict | None:
        try:
            payload = self._judge_payload(subject, record)
        except (SemanticJudgeTransportError, SemanticJudgeResponseError):
            return None
        if payload is None:  # pragma: no cover - defensive
            return None

        try:
            verdict = CompetitorVerdict(
                classification=payload.get("classification"),
                # Falls back to the page title rather than to an invented
                # company name.
                name=payload.get("name") or record.title,
                relevance_score=payload.get("relevance_score"),
                reason=payload.get("reason"),
            )
        except ValidationError as exc:
            self.response_failures.append(record.url)
            logger.warning(
                "web_evidence_verdict_rejected",
                extra={"domain": record.domain, "reason": str(exc)},
            )
            return None

        self.judged += 1
        return verdict


__all__ = [
    "OpenAICompetitorClassifier",
    "OpenAIDemandClassifier",
    "evidence_prompt",
]
