"""Turning one investigation into three families of search.

    RESEARCH      -> what is published about the underlying problem
    DEMAND        -> what people say about experiencing it
    COMPETITORS   -> what is already sold to address it

THE PLAN IS VALIDATED BEFORE ANY PROVIDER IS CONTACTED. That ordering is
the cost control and it is not negotiable: a model that answered with
thirty queries would otherwise buy thirty billable requests from one
click. `validate_plan` caps every family, and the caps are checked
against a total ceiling as well, so no combination of families can add up
to a surprise.

Research keeps the EXISTING gate. `app.research_intelligence.enrichment
.build_plan_with_fallback` already refuses plans built from an industry
name alone, and reusing it means the academic side of an investigation is
held to the identical standard as an opportunity's -- there is no second,
looser path to the arXiv collector.

Demand and competitor queries do NOT go through that gate, because it
encodes academic vocabulary ("urban freight", "matching markets") and
would reject the ordinary product language these families need. They get
their own validation, expressed below.
"""

import logging
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import Investigation
from app.investigations.subject import research_subject_from_investigation
from app.research_intelligence.enrichment import build_plan_with_fallback
from app.research_intelligence.query_generation import ResearchQueryGenerator
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject
from app.web_intelligence.execution import PlannedWebSearch
from app.web_intelligence.schemas import (
    DEFAULT_LOCALE,
    INDIA_LOCALE,
    MAX_QUERY_CHARS,
    SearchLocale,
    WebSearchFamily,
)

logger = logging.getLogger(__name__)

# V1 family sizes. Small on purpose: every web query is one billable
# provider request, and three angles per family is enough to see whether
# a market talks about a problem at all.
RESEARCH_QUERY_COUNT = 3
MIN_WEB_QUERIES_PER_FAMILY = 2
MAX_WEB_QUERIES_PER_FAMILY = 3

# The ceiling on ONE run's web spend, checked independently of the
# per-family caps so that adding a family later cannot quietly double it.
MAX_WEB_QUERIES_PER_PLAN = MAX_WEB_QUERIES_PER_FAMILY * len(WebSearchFamily)

# Two queries this similar are the same search. Jaccard over token sets,
# so reordering words does not disguise a repeat. Matches the research
# side's threshold.
NEAR_DUPLICATE_THRESHOLD = 0.8

# FAMILY INTENT, enforced rather than trusted.
#
# A generator -- deterministic or model-backed -- must not be able to
# answer the same three queries for all three families and have GapRadar
# report "we looked at demand and competitors". Each web family therefore
# has to show at least one marker of what it was asked, and competitor
# queries must not read as complaint searches.
_DEMAND_MARKERS = frozenset(
    {
        "problem", "problems", "challenge", "challenges", "issue", "issues",
        "struggle", "struggles", "struggling", "pain", "complaints", "complaint",
        "difficulty", "difficulties", "frustration", "why", "waste", "manual",
        "mistakes", "errors", "losing",
    }
)
_COMPETITOR_MARKERS = frozenset(
    {
        "software", "platform", "tool", "tools", "app", "apps", "solution",
        "solutions", "saas", "vendors", "vendor", "alternatives", "system",
        "systems", "product", "products", "automation", "pricing",
    }
)

# Words that say an investigation is about India. Deliberately a tiny,
# explicit list rather than any kind of inference: see `locale_for`.
_INDIA_MARKERS = frozenset(
    {"india", "indian", "bharat", "gst", "upi", "rupee", "rupees", "inr"}
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class InvestigationPlanRejectedError(Exception):
    """The plan is not worth spending provider requests on.

    Raised BEFORE acquisition, always. The message is safe to show an
    operator and never contains a credential or a prompt.
    """


class InvestigationPlan(BaseModel):
    """Everything one investigation run intends to ask, and why.

    Frozen and validated: an InvestigationPlan that exists has already
    passed `validate_plan`, so nothing downstream re-checks it and
    nothing can hand the providers an unvalidated one.

    `research_plan` is the existing ResearchQueryPlan, carried whole
    rather than flattened into a list of strings, because the research
    engine needs its concepts and rationale and re-deriving them here
    would create a second, quietly divergent reading.
    """

    model_config = ConfigDict(frozen=True)

    subject: ResearchSubject
    research_plan: ResearchQueryPlan
    demand_queries: list[str] = Field(default_factory=list)
    competitor_queries: list[str] = Field(default_factory=list)
    locale: SearchLocale = DEFAULT_LOCALE
    rationale: str = ""

    @property
    def research_queries(self) -> list[str]:
        return list(self.research_plan.queries)

    @property
    def web_searches(self) -> list[PlannedWebSearch]:
        """Every web search this plan calls for, in execution order.

        Demand first: if a run is going to be cut short, the evidence
        that a problem is real is worth more than the list of who else
        sells something.
        """
        return [
            *(
                PlannedWebSearch(query=query, family=WebSearchFamily.DEMAND)
                for query in self.demand_queries
            ),
            *(
                PlannedWebSearch(query=query, family=WebSearchFamily.COMPETITOR)
                for query in self.competitor_queries
            ),
        ]


# -- locale -----------------------------------------------------------------


def locale_for(subject: ResearchSubject) -> SearchLocale:
    """Which locale to search from. Retrieval configuration only.

    Deliberately NOT a geopolitical inference engine, and deliberately
    not a guess about where the user is sitting: GapRadar never reads an
    IP, a browser locale, or a timezone. It reads what the investigation
    says about itself, matches one explicit word list, and otherwise uses
    the documented default.

    The India case is the one the acquisition pilot earned. Its
    India-specific query returned nothing usable from us/en -- the
    provider rejected a redirect -- and returned eight organic results
    from in/en. So an investigation that names India is searched from
    India. Everything else is us/en, chosen rather than inferred so two
    identical investigations cannot return different evidence depending
    on where the backend happens to be deployed.
    """
    words = set(tokenize(f"{subject.problem} {subject.description}"))
    if subject.industry:
        words |= set(tokenize(subject.industry))
    if words & _INDIA_MARKERS:
        return INDIA_LOCALE
    return DEFAULT_LOCALE


# -- validation -------------------------------------------------------------


def _near_duplicate(first: str, second: str) -> bool:
    left, right = set(tokenize(first)), set(tokenize(second))
    if not left or not right:
        return False
    overlap = len(left & right) / len(left | right)
    return overlap >= NEAR_DUPLICATE_THRESHOLD


def validate_web_family(
    queries: list[str], *, family: WebSearchFamily, markers: frozenset[str]
) -> None:
    """Refuse a family that would spend provider requests on nothing.

    Every rule here exists because a generator could otherwise produce
    the shape it names:

    - count bounds, so a model cannot buy thirty requests;
    - non-blank and length-bounded, so prose is not submitted as a query;
    - no near-duplicates, so three requests are three angles rather than
      one paid for three times;
    - at least one family marker, so a "demand" family that is really
      three product searches cannot be reported as demand evidence.
    """
    if not MIN_WEB_QUERIES_PER_FAMILY <= len(queries) <= MAX_WEB_QUERIES_PER_FAMILY:
        raise InvestigationPlanRejectedError(
            f"the {family.value} family needs between "
            f"{MIN_WEB_QUERIES_PER_FAMILY} and {MAX_WEB_QUERIES_PER_FAMILY} "
            f"queries; it produced {len(queries)}"
        )

    for query in queries:
        if not query.strip():
            raise InvestigationPlanRejectedError(
                f"a generated {family.value} query was blank"
            )
        if len(query) > MAX_QUERY_CHARS:
            raise InvestigationPlanRejectedError(
                f"a generated {family.value} query is longer than "
                f"{MAX_QUERY_CHARS} characters; that is prose, not a query"
            )

    for index, query in enumerate(queries):
        for other in queries[index + 1 :]:
            if _near_duplicate(query, other):
                raise InvestigationPlanRejectedError(
                    f"two {family.value} queries are near-duplicates and would "
                    f"buy the same search twice: {query!r} and {other!r}"
                )

    for query in queries:
        if not set(tokenize(query)) & markers:
            raise InvestigationPlanRejectedError(
                f"the {family.value} query {query!r} does not read as a "
                f"{family.value} search; the family's intent was not preserved"
            )


def validate_plan(plan: InvestigationPlan) -> None:
    """Refuse the whole plan if any family is unusable. Never contacts anyone.

    The research family is NOT re-validated here: it already passed
    `app.research_intelligence.enrichment.validate_plan`, which is the
    same gate the opportunity path applies, and running a second,
    different check over it would create two definitions of an acceptable
    research plan.
    """
    if len(plan.research_plan.queries) != RESEARCH_QUERY_COUNT:
        raise InvestigationPlanRejectedError(
            f"a research plan must carry exactly {RESEARCH_QUERY_COUNT} "
            f"queries; it carried {len(plan.research_plan.queries)}"
        )

    validate_web_family(
        plan.demand_queries,
        family=WebSearchFamily.DEMAND,
        markers=_DEMAND_MARKERS,
    )
    validate_web_family(
        plan.competitor_queries,
        family=WebSearchFamily.COMPETITOR,
        markers=_COMPETITOR_MARKERS,
    )

    total = len(plan.demand_queries) + len(plan.competitor_queries)
    if total > MAX_WEB_QUERIES_PER_PLAN:
        raise InvestigationPlanRejectedError(
            f"this plan would run {total} web searches; the ceiling is "
            f"{MAX_WEB_QUERIES_PER_PLAN}"
        )


# -- generation -------------------------------------------------------------

# Deterministic demand and competitor templates.
#
# NOT LANGUAGE UNDERSTANDING, and they do not pretend to be. They take
# the nouns an investigation actually used and wrap them in the phrasing
# people search with when they are complaining about something, or
# shopping for something. That is enough to produce searchable queries
# for most ideas, it costs nothing, and it means the model is reached
# only for the wording these cannot serve.
_DEMAND_TEMPLATES = (
    "{topic} problems",
    "{topic} challenges",
    "why is {topic} so difficult",
)
_COMPETITOR_TEMPLATES = (
    "{topic} software",
    "{topic} platform",
    "best {topic} tools",
)

# Words that carry no retrieval signal in a product or complaint search.
_QUERY_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "than", "that",
        "this", "these", "those", "of", "for", "to", "in", "on", "at", "by",
        "with", "from", "into", "over", "is", "are", "was", "were", "be",
        "been", "being", "it", "its", "their", "there", "why", "how", "what",
        "when", "who", "can", "cannot", "do", "does", "did", "not", "no",
        "so", "still", "get", "got", "have", "has", "had", "my", "our",
        "your", "we", "they", "them", "you", "i", "me",
    }
)

# How many of the investigation's own words become the searchable topic.
# Three keeps a query specific enough to retrieve something and short
# enough not to retrieve nothing.
_TOPIC_WORDS = 3


class InvestigationWebQueryGenerator(Protocol):
    """Produces the demand and competitor families for one subject.

    A protocol so the model-backed generator is a drop-in for the
    deterministic one and this module never names a vendor.
    """

    def generate_web_queries(
        self, subject: ResearchSubject
    ) -> tuple[list[str], list[str]]: ...


class TemplateWebQueryGenerator:
    """Deterministic stand-in. Templates over the subject's own nouns.

    Where it is weak, stated plainly: an investigation whose wording is
    abstract ("a better way to do things") yields a topic that retrieves
    noise. That failure is VISIBLE -- the queries are in the plan and in
    the persisted execution rows -- rather than hidden behind a score,
    and it is exactly the case the model fallback exists for.
    """

    def __init__(self, *, per_family: int = MAX_WEB_QUERIES_PER_FAMILY) -> None:
        self.per_family = per_family

    def generate_web_queries(
        self, subject: ResearchSubject
    ) -> tuple[list[str], list[str]]:
        topic = self._topic(subject)
        # A single generic noun ("thing problems") is a wasted provider
        # request, not a narrow one. Two words is the minimum that can
        # retrieve something specific, and falling short here is exactly
        # what the model fallback exists for.
        if len(topic.split()) < 2:
            raise InvestigationPlanRejectedError(
                "no searchable topic could be derived from this "
                "investigation's wording"
            )
        demand = [
            template.format(topic=topic)
            for template in _DEMAND_TEMPLATES[: self.per_family]
        ]
        competitors = [
            template.format(topic=topic)
            for template in _COMPETITOR_TEMPLATES[: self.per_family]
        ]
        return demand, competitors

    def _topic(self, subject: ResearchSubject) -> str:
        """The investigation's own most specific words, in its own order.

        The industry is appended only when the problem alone is too thin
        to search: it broadens, and letting it lead would turn every
        investigation in a sector into the same query.
        """
        words = [
            word
            for word in tokenize(subject.problem)
            if word not in _QUERY_STOPWORDS and len(word) > 2
        ]
        if len(words) < 2 and subject.industry:
            words += [
                word
                for word in tokenize(subject.industry)
                if word not in _QUERY_STOPWORDS and len(word) > 2
            ]
        # Deduplicate while preserving order: a repeated noun in a query
        # is what the research gate calls malformed.
        seen: set[str] = set()
        topic_words: list[str] = []
        for word in words:
            if word not in seen:
                seen.add(word)
                topic_words.append(word)
            if len(topic_words) == _TOPIC_WORDS:
                break
        return " ".join(topic_words)


def build_investigation_plan(
    investigation: Investigation,
    *,
    research_generator: ResearchQueryGenerator | None = None,
    research_fallback: ResearchQueryGenerator | None = None,
    web_generator: InvestigationWebQueryGenerator | None = None,
    web_fallback: InvestigationWebQueryGenerator | None = None,
) -> InvestigationPlan:
    """The whole plan for one investigation, validated. Contacts nobody.

    Ordering, and the reason for it:

    1. RESEARCH first, through the existing gate. If the academic side
       cannot be planned the run has a real problem, and finding that out
       before spending anything on web searches is cheaper.
    2. WEB families deterministically, then the model only if the
       deterministic attempt is refused. Same cost control as the
       research side: free and predictable first, paid second.
    3. VALIDATE the assembled plan. Nothing has been searched at this
       point, so a bad plan costs one LLM call at most, never a provider
       request.

    Raises InvestigationPlanRejectedError when no stage could produce a
    usable plan, ResearchPlanUnavailableError / QueryGenerationProviderError
    from the research stage unchanged -- the caller already maps those
    onto the outcome taxonomy and a third error type would be a third
    thing to map.
    """
    subject = research_subject_from_investigation(investigation)

    research_plan = build_plan_with_fallback(
        subject, generator=research_generator, fallback=research_fallback
    )

    generator = web_generator or TemplateWebQueryGenerator()
    demand, competitors, source = _generate_web_families(
        subject, generator=generator, fallback=web_fallback
    )

    plan = InvestigationPlan(
        subject=subject,
        research_plan=research_plan,
        demand_queries=demand,
        competitor_queries=competitors,
        locale=locale_for(subject),
        rationale=(
            f"Research: {research_plan.rationale or 'generated'}. "
            f"Demand and competitor queries: {source} generator."
        ),
    )
    validate_plan(plan)
    logger.info(
        "investigation_plan_built",
        extra={
            "investigation_id": str(investigation.id),
            "research_queries": len(plan.research_queries),
            "demand_queries": len(plan.demand_queries),
            "competitor_queries": len(plan.competitor_queries),
            "locale": str(plan.locale),
            "web_generator": source,
        },
    )
    return plan


def _generate_web_families(
    subject: ResearchSubject,
    *,
    generator: InvestigationWebQueryGenerator,
    fallback: InvestigationWebQueryGenerator | None,
) -> tuple[list[str], list[str], str]:
    """Deterministic first; the model only if that attempt is refused.

    THE GATE APPLIES TO BOTH STAGES. A fallback family runs through the
    same `validate_web_family` that rejected the deterministic one, so a
    model cannot buy provider requests by returning three near-identical
    queries in nicer words.
    """
    deterministic_error: Exception | None = None
    try:
        demand, competitors = generator.generate_web_queries(subject)
        validate_web_family(
            demand, family=WebSearchFamily.DEMAND, markers=_DEMAND_MARKERS
        )
        validate_web_family(
            competitors,
            family=WebSearchFamily.COMPETITOR,
            markers=_COMPETITOR_MARKERS,
        )
        return demand, competitors, "deterministic"
    except InvestigationPlanRejectedError as exc:
        deterministic_error = exc
        logger.info(
            "[investigation] deterministic web queries rejected, trying "
            "fallback: %s",
            exc,
        )

    if fallback is None:
        raise InvestigationPlanRejectedError(str(deterministic_error))

    demand, competitors = fallback.generate_web_queries(subject)
    validate_web_family(
        demand, family=WebSearchFamily.DEMAND, markers=_DEMAND_MARKERS
    )
    validate_web_family(
        competitors, family=WebSearchFamily.COMPETITOR, markers=_COMPETITOR_MARKERS
    )
    return demand, competitors, "fallback"
