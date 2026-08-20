"""The semantic layer: taxonomies, clamping, and declining to judge.

No network. The deterministic classifiers are exercised directly; the
OpenAI ones are exercised through an httpx MockTransport so the real SDK
and the real prompt-building code run against a fake wire.
"""

import json
import uuid
from typing import Any

import httpx2
import openai
import pytest

from app.config import Settings
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    ResearchSubjectOrigin,
)
from app.integrations.openai.errors import SemanticJudgeUnavailableError
from app.integrations.openai.web_evidence import (
    OpenAICompetitorClassifier,
    OpenAIDemandClassifier,
    evidence_prompt,
)
from app.research_intelligence.schemas import ResearchSubject
from app.web_intelligence.classification import (
    CompetitorVerdict,
    DemandVerdict,
    LexicalCompetitorClassifier,
    LexicalDemandClassifier,
    clamp_score,
)
from app.web_intelligence.schemas import WebIntelligenceRecord

SUBJECT = ResearchSubject(
    subject_id=uuid.uuid4(),
    origin=ResearchSubjectOrigin.INVESTIGATION,
    problem="Independent restaurants waste food because inventory is manual",
    description="Owners guess par levels and over-order perishables.",
    industry="Hospitality",
)

ON_TOPIC = WebIntelligenceRecord(
    query="restaurant inventory waste problems",
    title="Restaurants waste food from manual inventory guesswork",
    url="https://a.test/1",
    domain="a.test",
    snippet="Independent restaurants report spoilage from manual inventory.",
    position=1,
)

PRODUCT = WebIntelligenceRecord(
    query="restaurant inventory software",
    title="Restaurant inventory software for independent kitchens",
    url="https://b.test/1",
    domain="b.test",
    snippet="Platform that forecasts food inventory and reduces waste.",
    position=1,
)

OFF_TOPIC = WebIntelligenceRecord(
    query="restaurant inventory waste problems",
    title="Quantum computing milestones in 2026",
    url="https://c.test/1",
    domain="c.test",
    snippet="Error correction thresholds were reached this year.",
    position=4,
)


# -- taxonomies -------------------------------------------------------------


def test_the_demand_taxonomy_has_exactly_five_members() -> None:
    assert {member.value for member in DemandEvidenceClassification} == {
        "strong_support",
        "support",
        "neutral",
        "contradicts",
        "irrelevant",
    }


def test_the_competitor_taxonomy_has_exactly_four_members() -> None:
    assert {member.value for member in CompetitorClassification} == {
        "direct",
        "adjacent",
        "substitute",
        "irrelevant",
    }


def test_contradicts_is_not_the_same_as_irrelevant() -> None:
    """A page saying the problem is solved is evidence, not noise."""
    assert DemandEvidenceClassification.CONTRADICTS.is_accepted
    assert not DemandEvidenceClassification.IRRELEVANT.is_accepted


def test_only_support_verdicts_count_as_supporting() -> None:
    supporting = {
        member
        for member in DemandEvidenceClassification
        if member.is_supporting
    }
    assert supporting == {
        DemandEvidenceClassification.STRONG_SUPPORT,
        DemandEvidenceClassification.SUPPORT,
    }


# -- verdict validation -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [(150, 100.0), (-3, 0.0), (87, 87.0), (87.5, 87.5)]
)
def test_a_score_is_clamped_into_range(raw: Any, expected: float) -> None:
    assert clamp_score(raw) == expected


@pytest.mark.parametrize("raw", ["87", None, True, float("inf"), float("nan")])
def test_a_non_numeric_score_becomes_none_never_zero(raw: Any) -> None:
    """"Could not say" and "said zero" are different answers."""
    assert clamp_score(raw) is None


def test_a_verdict_with_a_blank_reason_is_refused() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        DemandVerdict(
            classification=DemandEvidenceClassification.SUPPORT,
            relevance_score=50,
            reason="   ",
        )


def test_a_verdict_with_an_unknown_classification_is_refused() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        DemandVerdict(
            classification="extremely_supportive",
            relevance_score=50,
            reason="ok",
        )


def test_a_competitor_verdict_requires_a_name() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        CompetitorVerdict(
            classification=CompetitorClassification.DIRECT,
            relevance_score=50,
            name="  ",
            reason="ok",
        )


# -- the deterministic classifiers ------------------------------------------


def test_the_lexical_demand_classifier_accepts_an_on_topic_page() -> None:
    verdict = LexicalDemandClassifier().classify(subject=SUBJECT, record=ON_TOPIC)

    assert verdict is not None
    assert verdict.classification.is_accepted
    assert "Lexical overlap only" in verdict.reason


def test_the_lexical_demand_classifier_rejects_an_off_topic_page() -> None:
    verdict = LexicalDemandClassifier().classify(subject=SUBJECT, record=OFF_TOPIC)

    assert verdict is not None
    assert verdict.classification is DemandEvidenceClassification.IRRELEVANT


def test_the_lexical_demand_classifier_never_claims_strong_support() -> None:
    """A word count is not strong evidence of anything."""
    for record in (ON_TOPIC, PRODUCT, OFF_TOPIC):
        verdict = LexicalDemandClassifier().classify(subject=SUBJECT, record=record)
        assert verdict is not None
        assert verdict.classification is not (
            DemandEvidenceClassification.STRONG_SUPPORT
        )


def test_the_lexical_competitor_classifier_never_claims_a_direct_competitor() -> None:
    """Deciding that is reading the product, which discovery does not do."""
    for record in (ON_TOPIC, PRODUCT, OFF_TOPIC):
        verdict = LexicalCompetitorClassifier().classify(
            subject=SUBJECT, record=record
        )
        assert verdict is not None
        assert verdict.classification is not CompetitorClassification.DIRECT


def test_the_lexical_competitor_classifier_needs_product_language() -> None:
    problem_page = LexicalCompetitorClassifier().classify(
        subject=SUBJECT, record=ON_TOPIC
    )
    product_page = LexicalCompetitorClassifier().classify(
        subject=SUBJECT, record=PRODUCT
    )

    assert problem_page is not None and product_page is not None
    assert problem_page.classification is CompetitorClassification.IRRELEVANT
    assert product_page.classification is CompetitorClassification.ADJACENT


def test_a_competitor_name_is_always_the_page_title() -> None:
    verdict = LexicalCompetitorClassifier().classify(subject=SUBJECT, record=PRODUCT)
    assert verdict is not None
    assert verdict.name == PRODUCT.title


# -- the OpenAI classifiers -------------------------------------------------


class Wire:
    """A scripted OpenAI wire."""

    def __init__(self, payload: Any = None, *, raises: Exception | None = None):
        self.payload = payload
        self.raises = raises
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if self.raises is not None:
            raise self.raises
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
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


def client_for(wire: Wire) -> openai.OpenAI:
    return openai.OpenAI(
        api_key="test-key-do-not-log",
        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(wire)),
        max_retries=0,
    )


def settings() -> Settings:
    return Settings(_env_file=None, OPENAI_API_KEY="unused")


def test_the_prompt_says_the_page_was_not_opened() -> None:
    """The model must know it is reasoning from a search result."""
    prompt = evidence_prompt(SUBJECT, ON_TOPIC)

    assert "NOT opened" in prompt
    assert SUBJECT.problem in prompt
    assert ON_TOPIC.snippet in prompt


def test_the_prompt_carries_no_credential() -> None:
    assert "test-key-do-not-log" not in evidence_prompt(SUBJECT, ON_TOPIC)


def test_a_usable_demand_answer_becomes_a_verdict() -> None:
    wire = Wire(
        {
            "classification": "strong_support",
            "relevance_score": 91,
            "reason": "Owners describe over-ordering perishables weekly.",
        }
    )

    verdict = OpenAIDemandClassifier(
        client=client_for(wire), settings=settings()
    ).classify(subject=SUBJECT, record=ON_TOPIC)

    assert verdict is not None
    assert verdict.classification is DemandEvidenceClassification.STRONG_SUPPORT
    assert verdict.relevance_score == 91.0


def test_a_usable_competitor_answer_becomes_a_verdict() -> None:
    wire = Wire(
        {
            "classification": "direct",
            "name": "MarketMan",
            "relevance_score": 88,
            "reason": "Sells inventory forecasting to independent restaurants.",
        }
    )

    verdict = OpenAICompetitorClassifier(
        client=client_for(wire), settings=settings()
    ).classify(subject=SUBJECT, record=PRODUCT)

    assert verdict is not None
    assert verdict.classification is CompetitorClassification.DIRECT
    assert verdict.name == "MarketMan"


def test_a_missing_competitor_name_falls_back_to_the_page_title() -> None:
    """Never an invented company name."""
    wire = Wire(
        {
            "classification": "adjacent",
            "name": "",
            "relevance_score": 40,
            "reason": "Related tooling.",
        }
    )

    verdict = OpenAICompetitorClassifier(
        client=client_for(wire), settings=settings()
    ).classify(subject=SUBJECT, record=PRODUCT)

    assert verdict is not None
    assert verdict.name == PRODUCT.title


def test_an_out_of_range_score_is_clamped_not_rejected() -> None:
    wire = Wire(
        {"classification": "support", "relevance_score": 150, "reason": "ok"}
    )

    verdict = OpenAIDemandClassifier(
        client=client_for(wire), settings=settings()
    ).classify(subject=SUBJECT, record=ON_TOPIC)

    assert verdict is not None
    assert verdict.relevance_score == 100.0


def test_an_unusable_answer_declines_and_counts_as_a_failure() -> None:
    """A judge that malfunctioned has said nothing about the page."""
    wire = Wire({"classification": "vibes", "relevance_score": 50, "reason": "ok"})
    classifier = OpenAIDemandClassifier(
        client=client_for(wire), settings=settings()
    )

    verdict = classifier.classify(subject=SUBJECT, record=ON_TOPIC)

    assert verdict is None
    assert classifier.failures == 1


def test_a_transport_failure_declines_and_counts_as_a_failure() -> None:
    wire = Wire(raises=httpx2.ConnectError("no route"))
    classifier = OpenAIDemandClassifier(
        client=client_for(wire), settings=settings()
    )

    assert classifier.classify(subject=SUBJECT, record=ON_TOPIC) is None
    assert classifier.failures == 1


def test_declining_is_not_the_same_as_judging_irrelevant() -> None:
    """The counter is what tells orchestration the difference."""
    good = OpenAIDemandClassifier(
        client=client_for(
            Wire(
                {
                    "classification": "irrelevant",
                    "relevance_score": 2,
                    "reason": "Off topic.",
                }
            )
        ),
        settings=settings(),
    )
    verdict = good.classify(subject=SUBJECT, record=OFF_TOPIC)

    assert verdict is not None
    assert verdict.classification is DemandEvidenceClassification.IRRELEVANT
    assert good.failures == 0


def test_no_key_means_the_classifier_is_simply_unavailable() -> None:
    """Absent, not broken: the run degrades to the lexical classifiers."""
    with pytest.raises(SemanticJudgeUnavailableError):
        OpenAIDemandClassifier(settings=Settings(_env_file=None, OPENAI_API_KEY=""))
