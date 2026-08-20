"""The LLM query-generation fallback, with no network anywhere.

The real OpenAI SDK is driven through an httpx2.MockTransport, so what
these tests exercise is the actual client and the actual prompt-building
code -- only the wire is fake. A test that reached the API would fail on
the unrouted-request assertion rather than silently spend money.

WHY THIS FILE EXISTS. The fallback had NO test that constructed it
against a live context, and as a result two defects sat on the path
undetected: `plan_prompt` read an attribute the context type has never
defined (AttributeError on every call), and the returned plan was built
without its required id (ValidationError on every call). Either one
meant the fallback could never have rescued a single opportunity in
production. These tests pin both.
"""

import json
import uuid
from typing import Any

import httpx2
import openai
import pytest

from app.config import Settings
from app.domain.enums import ResearchSubjectOrigin
from app.integrations.openai.errors import SemanticJudgeUnavailableError
from app.integrations.openai.query_generator import (
    LlmQueryPlanError,
    OpenAIResearchQueryGenerator,
    plan_prompt,
)
from app.research_intelligence.schemas import ResearchSubject

SUBJECT = ResearchSubject(
    subject_id=uuid.uuid4(),
    origin=ResearchSubjectOrigin.SIGNAL,
    problem="Why is booking cargo vehicles harder than passenger transport?",
    description="Shippers negotiate with unorganized drivers at inflated prices.",
    industry="Logistics",
)

GOOD_PLAN = {
    "queries": ["urban freight matching", "dynamic vehicle routing", "spot pricing"],
    "concepts": ["urban freight", "matching markets"],
}


class Wire:
    """A scripted OpenAI wire, recording every request body it served."""

    def __init__(self, payload: Any = None) -> None:
        self.payload = GOOD_PLAN if payload is None else payload
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
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
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(self.payload),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 800,
                    "completion_tokens": 120,
                    "total_tokens": 920,
                },
            },
        )


def generator_for(wire: Wire) -> OpenAIResearchQueryGenerator:
    client = openai.OpenAI(
        api_key="test-key-do-not-log",
        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(wire)),
        max_retries=0,
    )
    return OpenAIResearchQueryGenerator(
        client=client, settings=Settings(_env_file=None, OPENAI_API_KEY="unused")
    )


# -- the prompt -------------------------------------------------------------


def test_the_prompt_states_the_problem() -> None:
    """REGRESSION: this read a field the contract does not define."""
    prompt = plan_prompt(SUBJECT)

    assert SUBJECT.problem in prompt
    assert SUBJECT.description in prompt
    assert SUBJECT.industry in prompt


def test_the_prompt_works_without_an_industry() -> None:
    prompt = plan_prompt(SUBJECT.model_copy(update={"industry": None}))

    assert SUBJECT.problem in prompt
    assert "INDUSTRY" not in prompt


def test_the_prompt_carries_no_credential() -> None:
    assert "test-key-do-not-log" not in plan_prompt(SUBJECT)


# -- the plan ---------------------------------------------------------------


def test_a_usable_answer_becomes_a_plan() -> None:
    """REGRESSION: the plan was constructed without its required id."""
    plan = generator_for(Wire()).generate(SUBJECT)

    assert plan.queries == GOOD_PLAN["queries"]
    assert plan.concepts == GOOD_PLAN["concepts"]


def test_the_plan_is_attributed_to_the_subject_it_was_built_for() -> None:
    """Without this the plan cannot be persisted against anything."""
    plan = generator_for(Wire()).generate(SUBJECT)

    assert plan.subject_id == SUBJECT.subject_id


def test_the_generator_works_for_an_investigation_subject() -> None:
    """One generator, two subjects -- there is no signal-only path here."""
    investigation = SUBJECT.model_copy(
        update={
            "subject_id": uuid.uuid4(),
            "origin": ResearchSubjectOrigin.INVESTIGATION,
        }
    )

    plan = generator_for(Wire()).generate(investigation)

    assert plan.subject_id == investigation.subject_id


def test_too_few_queries_is_refused() -> None:
    """An honest refusal is cheap; three useless provider jobs are not."""
    wire = Wire({"queries": ["only one"], "concepts": ["urban freight"]})

    with pytest.raises(LlmQueryPlanError):
        generator_for(wire).generate(SUBJECT)


def test_no_concepts_is_refused() -> None:
    wire = Wire({"queries": GOOD_PLAN["queries"], "concepts": []})

    with pytest.raises(LlmQueryPlanError):
        generator_for(wire).generate(SUBJECT)


# -- construction -----------------------------------------------------------


def test_no_key_means_the_fallback_is_simply_unavailable() -> None:
    """Absent, not broken: the caller degrades rather than crashing."""
    with pytest.raises(SemanticJudgeUnavailableError):
        OpenAIResearchQueryGenerator(settings=Settings(_env_file=None, OPENAI_API_KEY=""))
