"""The semantic judge, with no network anywhere.

The real OpenAI SDK is driven through an httpx2.MockTransport, so what
these tests exercise is the actual client and the actual request shape --
only the wire is fake. A test that reached the API would fail on the
unrouted-request assertion rather than silently spend money.
"""

import json
from typing import Any

import httpx2
import openai
import pytest

from app.config import Settings
from app.integrations.openai.errors import SemanticJudgeUnavailableError
from app.integrations.openai.semantic_matcher import (
    VERDICT_SCHEMA,
    OpenAISemanticMatcher,
    judge_prompt,
)
from app.research_intelligence.matching import (
    DEFAULT_RELEVANCE_THRESHOLD,
    ResearchMatchVerdict,
)
from tests.research_intelligence.test_candidates import CONTEXT, PLAN, RELEVANT

STRONG = {
    "relevance_score": 88,
    "matched_concepts": ["urban freight", "dynamic capacity allocation"],
    "match_reason": "Directly addresses on-demand freight vehicle assignment.",
    "technical_readiness_score": 72,
}


class Wire:
    """A scripted OpenAI wire, recording every request body it served."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status: int = 200,
        text: str | None = None,
        finish_reason: str = "stop",
        refusal: str | None = None,
        raises: Exception | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.payload = STRONG if payload is None else payload
        self.status = status
        self.text = text
        self.finish_reason = finish_reason
        self.refusal = refusal
        self.raises = raises
        self.usage = usage or {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "completion_tokens_details": {"reasoning_tokens": 180},
        }
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if self.raises is not None:
            raise self.raises
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "nope"}})
        content = self.text if self.text is not None else json.dumps(self.payload)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if self.refusal is not None:
            message["refusal"] = self.refusal
            message["content"] = None
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": self.finish_reason,
                    }
                ],
                "usage": self.usage,
            },
        )


def matcher_for(wire: Wire) -> OpenAISemanticMatcher:
    client = openai.OpenAI(
        api_key="test-key-do-not-log",
        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(wire)),
        max_retries=0,
    )
    return OpenAISemanticMatcher(
        client=client, settings=Settings(_env_file=None, OPENAI_API_KEY="unused")
    )


def judge(wire: Wire) -> ResearchMatchVerdict | None:
    return matcher_for(wire).judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)


# -- construction -----------------------------------------------------------


def test_without_a_key_the_matcher_refuses_to_be_built() -> None:
    """A matcher that cannot work must not look like a very harsh judge."""
    with pytest.raises(SemanticJudgeUnavailableError):
        OpenAISemanticMatcher(settings=Settings(_env_file=None, OPENAI_API_KEY=""))


def test_the_configured_model_and_reasoning_effort_are_used() -> None:
    wire = Wire()
    matcher_for(wire).judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    body = wire.requests[0]
    assert body["model"] == "gpt-5-mini"
    assert body["reasoning_effort"] == "medium"


def test_reasoning_model_parameters_are_respected() -> None:
    """gpt-5-mini rejects `max_tokens` and a non-default `temperature`."""
    wire = Wire()
    matcher_for(wire).judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    body = wire.requests[0]
    assert "max_completion_tokens" in body
    assert "max_tokens" not in body
    assert "temperature" not in body


def test_a_strict_json_schema_is_requested() -> None:
    """Structured output, so the adapter never parses prose."""
    wire = Wire()
    matcher_for(wire).judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    fmt = wire.requests[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "relevance_score",
        "matched_concepts",
        "match_reason",
        "technical_readiness_score",
    }
    # Strict mode expresses optionality as a nullable union.
    assert schema["properties"]["technical_readiness_score"]["type"] == [
        "integer",
        "null",
    ]


def test_the_schema_leaves_range_checking_to_the_verdict() -> None:
    """Strict mode rejects minimum/maximum, and the verdict clamps anyway."""
    score = VERDICT_SCHEMA["properties"]["relevance_score"]

    assert "minimum" not in score
    assert "maximum" not in score


# -- the rubric reaches the model ------------------------------------------


def test_the_prompt_carries_the_problem_the_paper_and_the_concepts() -> None:
    prompt = judge_prompt(CONTEXT, PLAN, RELEVANT)

    assert CONTEXT.problem in prompt
    assert CONTEXT.industry in prompt
    assert RELEVANT.title in prompt
    assert RELEVANT.abstract in prompt
    assert "urban freight" in prompt
    # The problem is stated before the paper, so the paper is read against it.
    assert prompt.index("MARKET PROBLEM") < prompt.index("PAPER TITLE")


def test_the_system_prompt_forbids_word_matching_and_industry_matching() -> None:
    wire = Wire()
    matcher_for(wire).judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    system = wire.requests[0]["messages"][0]["content"]
    assert "same words" in system
    assert "same industry" in system
    for band in ("90-100", "70-79", "0-24"):
        assert band in system


# -- verdicts ---------------------------------------------------------------


def test_a_strong_direct_match_is_returned_intact() -> None:
    verdict = judge(Wire(STRONG))

    assert verdict is not None
    assert verdict.relevance_score == 88.0
    assert verdict.matched_concepts == ["urban freight", "dynamic capacity allocation"]
    assert verdict.match_reason.startswith("Directly addresses")
    assert verdict.technical_readiness_score == 72.0
    assert verdict.relevance_score >= DEFAULT_RELEVANCE_THRESHOLD


def test_an_indirect_match_scores_in_the_middle_band() -> None:
    verdict = judge(
        Wire({**STRONG, "relevance_score": 58, "technical_readiness_score": None})
    )

    assert verdict is not None
    assert verdict.relevance_score == 58.0
    assert verdict.relevance_score < DEFAULT_RELEVANCE_THRESHOLD


def test_an_irrelevant_paper_scores_low_and_is_still_a_verdict() -> None:
    """Judged irrelevant is a verdict; it is not the same as declining."""
    verdict = judge(
        Wire(
            {
                "relevance_score": 8,
                "matched_concepts": [],
                "match_reason": "Protein folding has no bearing on freight booking.",
                "technical_readiness_score": None,
            }
        )
    )

    assert verdict is not None
    assert verdict.relevance_score == 8.0
    assert verdict.matched_concepts == []


@pytest.mark.parametrize(
    ("raw", "expected"), [(0, 0.0), (100, 100.0), (73, 73.0), (150, 100.0), (-5, 0.0)]
)
def test_scores_are_preserved_across_the_band_and_clamped_outside_it(
    raw: int, expected: float
) -> None:
    verdict = judge(Wire({**STRONG, "relevance_score": raw}))

    assert verdict is not None
    assert verdict.relevance_score == expected


def test_a_null_technical_readiness_is_preserved() -> None:
    """Null means "not assessed", and must survive as null."""
    verdict = judge(Wire({**STRONG, "technical_readiness_score": None}))

    assert verdict is not None
    assert verdict.technical_readiness_score is None


def test_concepts_are_normalized_by_the_verdict() -> None:
    verdict = judge(
        Wire(
            {
                **STRONG,
                "matched_concepts": [
                    "  urban freight ",
                    "Urban Freight",
                    "",
                    "vehicle routing",
                ],
            }
        )
    )

    assert verdict is not None
    assert verdict.matched_concepts == ["urban freight", "vehicle routing"]


def test_token_usage_is_accumulated_for_cost_reporting() -> None:
    matcher = matcher_for(Wire())
    matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)
    matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    assert matcher.prompt_tokens == 2400
    assert matcher.completion_tokens == 600
    assert matcher.reasoning_tokens == 360


# -- failures decline, and never score zero --------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_provider_failure_declines_rather_than_scoring_zero(status: int) -> None:
    """A network error is not evidence that research is irrelevant."""
    wire = Wire(status=status)
    matcher = matcher_for(wire)

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.failures == 1
    assert matcher.transport_failures == [RELEVANT.arxiv_id]


def test_a_quota_or_billing_failure_declines_and_is_reported() -> None:
    """429 insufficient_quota must not look like an irrelevant paper."""
    matcher = matcher_for(Wire(status=429))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.transport_failures == [RELEVANT.arxiv_id]


def test_a_timeout_declines() -> None:
    matcher = matcher_for(Wire(raises=httpx2.TimeoutException("too slow")))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.transport_failures == [RELEVANT.arxiv_id]


def test_a_connection_error_declines() -> None:
    matcher = matcher_for(Wire(raises=httpx2.ConnectError("no route")))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.failures == 1


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"relevance_score": "eighty"}',
        '{"matched_concepts": [], "match_reason": "x"}',
        '{"relevance_score": 80, "match_reason": "   "}',
        '{"relevance_score": null, "match_reason": "x"}',
    ],
)
def test_malformed_output_is_a_matcher_failure_not_a_zero(body: str) -> None:
    """The rule that keeps a broken judge from condemning good research."""
    matcher = matcher_for(Wire(text=body))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.failures == 1


def test_a_truncated_response_declines() -> None:
    """Reasoning tokens can exhaust the ceiling before the answer lands."""
    matcher = matcher_for(Wire(finish_reason="length"))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.response_failures == [RELEVANT.arxiv_id]


def test_a_refusal_declines() -> None:
    matcher = matcher_for(Wire(refusal="I cannot help with that."))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.response_failures == [RELEVANT.arxiv_id]


def test_a_content_filter_declines() -> None:
    matcher = matcher_for(Wire(finish_reason="content_filter"))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None
    assert matcher.failures == 1


def test_an_empty_response_declines() -> None:
    matcher = matcher_for(Wire(text=""))

    assert matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT) is None


def test_no_api_key_appears_in_any_failure_message() -> None:
    matcher = matcher_for(Wire(status=401))
    matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    assert all("test-key-do-not-log" not in f for f in matcher.transport_failures)


def test_a_successful_judgement_is_counted() -> None:
    matcher = matcher_for(Wire())
    matcher.judge(subject=CONTEXT, plan=PLAN, paper=RELEVANT)

    assert matcher.judged == 1
    assert matcher.failures == 0
