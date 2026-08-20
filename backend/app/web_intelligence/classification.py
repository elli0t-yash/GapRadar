"""Judging what a discovered page means for one investigation.

The layer above acquisition, and strictly separate from it: acquisition
says what was retrieved, this says what it is worth. A provider swap
cannot change a verdict, and a prompt change cannot change what was
retrieved.

Mirrors app.research_intelligence.matching in shape and in honesty:

- protocols the real LLM classifiers satisfy;
- verdict models that clamp and normalize whatever a classifier returns,
  because an LLM will eventually answer 150, an empty reason, or a
  category that does not exist;
- deterministic stand-ins that make the whole pipeline runnable and
  testable today and are explicitly NOT semantic.

Returning None from a classifier means it DECLINED to judge -- it could
not reach an opinion, the provider errored, the answer was unusable. That
is different from judging a page irrelevant, which is a verdict. Nothing
is persisted for a declined judgement.
"""

import math
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
)
from app.research_intelligence.schemas import ResearchSubject
from app.web_intelligence.schemas import WebIntelligenceRecord

# Same 0-100 band as opportunity_score and research relevance, so nothing
# downstream has to remember which scale it is holding.
MIN_SCORE = 0.0
MAX_SCORE = 100.0


def clamp_score(value: Any) -> float | None:
    """Force a score into 0-100, or None if it is not a number at all.

    THE APPLICATION-LAYER GUARANTEE, identical to the research side's. A
    non-numeric value becomes None, never 0: "could not say" and "said
    zero" are different answers.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(MIN_SCORE, min(MAX_SCORE, number))


class _WebVerdict(BaseModel):
    """Shared validation for both verdict kinds."""

    model_config = ConfigDict(frozen=True)

    # How strongly this page bears on the investigation, 0-100. Required:
    # a verdict with no strength is an assertion with no evidence.
    relevance_score: float = Field(ge=MIN_SCORE, le=MAX_SCORE)
    # Why. Must be about THIS investigation, not a summary of the page.
    reason: str

    @field_validator("relevance_score", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float | None:
        return clamp_score(value)

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reason must be a non-blank string")
        return " ".join(value.split())


class DemandVerdict(_WebVerdict):
    """One judgement about whether a page evidences the stated problem."""

    classification: DemandEvidenceClassification


class CompetitorVerdict(_WebVerdict):
    """One judgement about how a discovered product relates to the idea.

    `name` is the display identity. It is the page TITLE by default and
    is never a company name this system invented: extracting a reliable
    vendor name from a SERP snippet is not something discovery can do, and
    a confidently wrong company name is worse than an honest page title.
    """

    classification: CompetitorClassification
    name: str

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("name must be a non-blank string")
        return " ".join(value.split())


@runtime_checkable
class ReportsClassificationFailures(Protocol):
    """A classifier that can say how often it FAILED, not merely declined.

    Optional, and for the same reason as the research side's equivalent:
    a judge that never answered must not look like a judge that answered
    "irrelevant" every time. A deterministic classifier has no failures
    and does not implement this; callers treat its absence as zero.
    """

    @property
    def failures(self) -> int: ...


class DemandClassifier(Protocol):
    """Judges whether one page evidences one investigation's problem."""

    def classify(
        self, *, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> DemandVerdict | None: ...


class CompetitorClassifier(Protocol):
    """Judges how one discovered page relates to one investigation's idea."""

    def classify(
        self, *, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> CompetitorVerdict | None: ...


# -- deterministic stand-ins ------------------------------------------------
#
# NOT SEMANTIC, and they say so. They exist so the pipeline can be built,
# demonstrated and tested end to end before an LLM is wired in, and so the
# real classifiers have a baseline they must visibly beat.

# Words that appear in pages describing a problem being experienced.
_PAIN_MARKERS = frozenset(
    {
        "problem", "problems", "challenge", "challenges", "issue", "issues",
        "struggle", "struggles", "struggling", "pain", "difficult", "difficulty",
        "waste", "wasted", "inefficient", "manual", "error", "errors", "mistake",
        "mistakes", "complaint", "complaints", "frustrating", "frustration",
        "costly", "losing", "loss",
    }
)

# Words that mark a page as offering a product rather than describing a
# problem.
_PRODUCT_MARKERS = frozenset(
    {
        "software", "platform", "tool", "tools", "app", "solution", "solutions",
        "saas", "system", "systems", "pricing", "vendor", "vendors", "product",
        "products", "suite", "automation", "dashboard", "api",
    }
)


def _tokens(text: str) -> set[str]:
    return {word for word in "".join(
        character if character.isalnum() else " " for character in text.lower()
    ).split() if len(word) > 2}


def _subject_tokens(subject: ResearchSubject) -> set[str]:
    tokens = _tokens(subject.problem)
    if subject.industry:
        tokens |= _tokens(subject.industry)
    return tokens


def _overlap_score(subject: ResearchSubject, record: WebIntelligenceRecord) -> float:
    """Fraction of the subject's vocabulary the page covers, as 0-100."""
    subject_words = _subject_tokens(subject)
    if not subject_words:
        return 0.0
    page_words = _tokens(f"{record.title} {record.snippet}")
    return round(100.0 * len(subject_words & page_words) / len(subject_words), 2)


class LexicalDemandClassifier:
    """Deterministic stand-in. Counts shared words; understands nothing.

    It cannot tell that "food going off before service" and "spoilage in
    perishable inventory" are the same complaint, which is most of what
    demand classification exists to catch. Its verdicts are deliberately
    conservative: it never returns STRONG_SUPPORT, because a word count
    is not strong evidence of anything, and it never returns CONTRADICTS,
    because recognising a contradiction requires reading a claim.
    """

    # Below this the page is not about the investigated problem at all.
    irrelevant_below: float = 15.0
    # At or above this, and carrying pain vocabulary, it is support.
    support_at: float = 30.0

    def classify(
        self, *, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> DemandVerdict | None:
        score = _overlap_score(subject, record)
        page_words = _tokens(f"{record.title} {record.snippet}")
        has_pain_language = bool(page_words & _PAIN_MARKERS)

        if score < self.irrelevant_below:
            classification = DemandEvidenceClassification.IRRELEVANT
        elif score >= self.support_at and has_pain_language:
            classification = DemandEvidenceClassification.SUPPORT
        else:
            classification = DemandEvidenceClassification.NEUTRAL

        return DemandVerdict(
            classification=classification,
            relevance_score=score,
            reason=(
                f"Lexical overlap only: this page shares {score}% of the "
                f"problem's vocabulary and "
                + ("does" if has_pain_language else "does not")
                + " use problem language. No semantic judgement was made."
            ),
        )


class LexicalCompetitorClassifier:
    """Deterministic stand-in. Product vocabulary, not market understanding.

    It never returns DIRECT. Deciding that something solves the same
    problem for the same buyer requires reading the product, and claiming
    a direct competitor on the strength of the word "software" appearing
    in a title is exactly the false confidence this project refuses.
    """

    irrelevant_below: float = 15.0

    def classify(
        self, *, subject: ResearchSubject, record: WebIntelligenceRecord
    ) -> CompetitorVerdict | None:
        score = _overlap_score(subject, record)
        page_words = _tokens(f"{record.title} {record.snippet}")
        looks_like_a_product = bool(page_words & _PRODUCT_MARKERS)

        if score < self.irrelevant_below or not looks_like_a_product:
            classification = CompetitorClassification.IRRELEVANT
        else:
            classification = CompetitorClassification.ADJACENT

        return CompetitorVerdict(
            classification=classification,
            relevance_score=score,
            # The page title, never an invented company name.
            name=record.title,
            reason=(
                f"Lexical overlap only: this page shares {score}% of the "
                f"idea's vocabulary and "
                + ("does" if looks_like_a_product else "does not")
                + " read as a product. No semantic judgement was made."
            ),
        )


def classification_failures(classifier: object) -> int:
    """How many times this classifier has failed, or 0 if it cannot say."""
    if isinstance(classifier, ReportsClassificationFailures):
        return classifier.failures
    return 0
