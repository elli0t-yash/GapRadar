"""Turn one market pain into research-language search queries.

The translation problem this solves: people describe pain in consumer
terms ("booking cargo vehicles is hard") and researchers publish under
method terms ("dynamic vehicle routing"). Searching arXiv with the
problem statement verbatim finds nothing, so the statement has to be
re-expressed in the vocabulary the literature actually uses.

Nothing here touches a provider, a database, or a network. It reads a
MarketContext and returns a plan; acquisition is somebody else's job.
"""

import re
from typing import Protocol

from app.research_intelligence.schemas import MarketContext, ResearchQueryPlan

# Exactly three per opportunity for v1: enough angles to catch adjacent
# literature, few enough that a fan-out stays cheap and explainable.
QUERIES_PER_OPPORTUNITY = 3

# ResearchSearchRun.query is String(512); queries are far shorter than
# this in practice, but a generator that produced an over-long one would
# otherwise fail at INSERT time rather than here.
MAX_QUERY_LENGTH = 512

# Two normalized queries this similar are the same search. Jaccard over
# token sets, so word order does not disguise a repeat.
NEAR_DUPLICATE_THRESHOLD = 0.8

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Words that carry no retrieval signal. Deliberately small: this is a
# stoplist for query building, not a linguistic model.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "into",
        "over",
        "under",
        "about",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "why",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "how",
        "not",
        "no",
        "nor",
        "so",
        "such",
        "can",
        "cannot",
        "could",
        "should",
        "would",
        "will",
        "just",
        "very",
        "more",
        "most",
        "other",
        "others",
        "some",
        "any",
        "all",
        "both",
        "each",
        "few",
        "own",
        "same",
        "too",
        "only",
        "its",
        "it",
        "their",
        "there",
        "they",
        "them",
        "our",
        "your",
        "my",
        "we",
        "you",
        "i",
        "he",
        "she",
        "his",
        "her",
        "easily",
        "through",
        "like",
        "harder",
        "hard",
        "easy",
        "good",
        "bad",
        "new",
        "old",
        "make",
        "makes",
        "making",
        "get",
        "gets",
        "getting",
        "need",
        "needs",
        "needed",
        "want",
        "wants",
        "using",
        "use",
        "used",
    ]
)


class ResearchQueryGenerationError(Exception):
    """The context did not yield enough distinct research queries.

    Raised rather than padding the plan with a repeat or a placeholder: a
    duplicated query costs a real provider run and returns the same
    papers twice, which is worse than admitting the input was too thin.
    """


class ResearchQueryProviderError(Exception):
    """A generator's backing service could not be reached.

    Separate from ResearchQueryGenerationError, which means "this input
    yields no good queries" -- a conclusion. This one means no conclusion
    was reached at all, which is the only difference that matters when
    deciding whether a retry could help.

    Declared HERE, in the port, so an adapter can report a transport
    failure without the research core importing a vendor's exception
    types. Nothing in this package may import app.integrations.
    """


class ResearchQueryGenerator(Protocol):
    """Produces the research queries for one opportunity.

    A protocol, not a base class, so the eventual LLM-backed generator is
    a drop-in for the deterministic one and the orchestration never names
    a vendor. Implementations must be pure with respect to the database
    and must not perform acquisition.
    """

    def generate(self, context: MarketContext) -> ResearchQueryPlan: ...


# -- the deterministic generator --------------------------------------------

# Pain vocabulary -> the terminology the literature publishes under.
# Explicit and auditable on purpose: every query this generator produces
# can be traced to a line in this table. It is a lookup, not language
# understanding, and it is not presented as one.
_DOMAIN_LEXICON: dict[str, str] = {
    # freight, fleets, movement
    "cargo": "urban freight",
    "freight": "urban freight",
    "tempo": "urban freight",
    "truck": "freight transport",
    "trucks": "freight transport",
    "shipping": "freight logistics",
    "delivery": "last-mile delivery",
    "deliveries": "last-mile delivery",
    "courier": "last-mile delivery",
    "warehouse": "warehouse operations",
    "fleet": "fleet management",
    "vehicle": "vehicle routing",
    "vehicles": "vehicle routing",
    "route": "vehicle routing",
    "routing": "vehicle routing",
    "driver": "driver dispatch",
    "drivers": "driver dispatch",
    "dispatch": "driver dispatch",
    "ride": "ride sharing",
    "traffic": "traffic flow",
    "transport": "transportation networks",
    "transportation": "transportation networks",
    "mobility": "urban mobility",
    # markets, matching, pricing
    "booking": "on-demand allocation",
    "book": "on-demand allocation",
    "reservation": "on-demand allocation",
    "match": "matching markets",
    "matching": "matching markets",
    "marketplace": "two-sided markets",
    "price": "dynamic pricing",
    "prices": "dynamic pricing",
    "pricing": "dynamic pricing",
    "fare": "dynamic pricing",
    "negotiate": "automated negotiation",
    "transparency": "market transparency",
    "auction": "auction design",
    # money
    "payment": "payment systems",
    "payments": "payment systems",
    "invoice": "billing automation",
    "billing": "billing automation",
    "credit": "credit risk modelling",
    "loan": "credit scoring",
    "lending": "credit scoring",
    "fraud": "fraud detection",
    "settlement": "payment settlement",
    # operations
    "inventory": "inventory management",
    "supply": "supply chain",
    "stock": "inventory management",
    "demand": "demand forecasting",
    "schedule": "scheduling optimization",
    "scheduling": "scheduling optimization",
    "queue": "queueing theory",
    "waiting": "queueing theory",
    "capacity": "capacity planning",
    "allocation": "resource allocation",
    "workflow": "process optimization",
    # sectors
    "patient": "clinical workflow",
    "patients": "clinical workflow",
    "doctor": "clinical decision support",
    "diagnosis": "clinical diagnosis",
    "hospital": "healthcare operations",
    "student": "learning analytics",
    "students": "learning analytics",
    "teacher": "education technology",
    "course": "curriculum modelling",
    "farm": "precision agriculture",
    "farmer": "precision agriculture",
    "crop": "crop yield prediction",
    "soil": "soil monitoring",
    "energy": "energy systems",
    "grid": "smart grid",
    "solar": "renewable energy forecasting",
    "rent": "housing markets",
    "rental": "housing markets",
    "property": "real estate analytics",
    "retail": "retail demand",
    "customer": "customer behaviour modelling",
    "customers": "customer behaviour modelling",
    "recommend": "recommender systems",
    "recommendation": "recommender systems",
    "search": "information retrieval",
    "document": "document understanding",
    "support": "service operations",
    "waste": "waste management",
    "water": "water resource management",
}

# Industry -> domain terms, applied in addition to the text lexicon so
# the sector is represented even when the wording is generic.
_INDUSTRY_LEXICON: dict[str, tuple[str, ...]] = {
    "logistics": ("urban logistics", "freight transport"),
    "transportation": ("transportation networks", "urban mobility"),
    "mobility": ("urban mobility", "vehicle routing"),
    "fintech": ("financial technology", "payment systems"),
    "finance": ("financial technology", "credit risk modelling"),
    "banking": ("payment systems", "credit risk modelling"),
    "insurance": ("risk modelling", "actuarial analytics"),
    "healthcare": ("healthcare operations", "clinical decision support"),
    "health": ("healthcare operations", "clinical workflow"),
    "education": ("education technology", "learning analytics"),
    "agriculture": ("precision agriculture", "crop yield prediction"),
    "energy": ("energy systems", "smart grid"),
    "retail": ("retail demand", "inventory management"),
    "ecommerce": ("online marketplaces", "recommender systems"),
    "e-commerce": ("online marketplaces", "recommender systems"),
    "real estate": ("real estate analytics", "housing markets"),
    "manufacturing": ("production scheduling", "industrial optimization"),
    "b2b services": ("service operations", "process optimization"),
    "saas": ("software analytics", "service operations"),
    "media": ("content recommendation", "information retrieval"),
    "telecom": ("network optimization", "resource allocation"),
}

# Method vocabulary used to build the second and third angles, and to
# fall back on when the text yields only one domain term.
_METHOD_TERMS: tuple[str, ...] = (
    "optimization",
    "demand forecasting",
    "resource allocation",
    "predictive modelling",
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords and 1-2 char words removed.

    Shared with the candidate pre-filter so query building and candidate
    ranking cannot disagree about what a token is.
    """
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def normalize_query(query: str) -> str:
    """Collapse a query to its comparable form: lowercase, single-spaced."""
    return " ".join(query.lower().split())


def _repeats_a_token(query: str) -> bool:
    """Whether a query says the same word twice.

    Produced when a domain term collides with the method term appended to
    it -- "demand forecasting" + "demand forecasting", "process
    optimization" + "optimization". The result is not a narrower search,
    it is a malformed one, and it costs a real provider run.
    """
    tokens = query.split()
    return len(tokens) != len(set(tokens))


def _is_near_duplicate(candidate: str, accepted: list[str]) -> bool:
    """Whether `candidate` says the same thing as something already kept.

    Jaccard over token sets, so "urban freight routing" and "routing
    urban freight" are recognised as one query rather than two.
    """
    candidate_tokens = set(candidate.split())
    if not candidate_tokens:
        return True
    for existing in accepted:
        existing_tokens = set(existing.split())
        union = candidate_tokens | existing_tokens
        if not union:
            continue
        if len(candidate_tokens & existing_tokens) / len(union) >= (
            NEAR_DUPLICATE_THRESHOLD
        ):
            return True
    return False


def select_queries(candidates: list[str], *, limit: int) -> list[str]:
    """Normalize, drop blanks and near-duplicates, keep the first `limit`.

    Order is preserved: candidates are supplied best-angle-first, and a
    later duplicate never displaces an earlier distinct query.
    """
    selected: list[str] = []
    for candidate in candidates:
        normalized = normalize_query(candidate)
        if not normalized or len(normalized) > MAX_QUERY_LENGTH:
            continue
        if _repeats_a_token(normalized):
            continue
        if _is_near_duplicate(normalized, selected):
            continue
        selected.append(normalized)
        if len(selected) == limit:
            break
    return selected


class ConceptQueryGenerator:
    """Lexicon-driven translation from pain wording to research wording.

    DETERMINISTIC, AND NOT SEMANTIC. It maps known pain vocabulary onto
    known research vocabulary through the tables above; it does not
    understand the problem. It exists so the whole research pipeline can
    be built, tested and demonstrated before an LLM generator is wired in,
    and so there is a reference implementation the LLM one must beat.

    Where it will be weak, stated plainly: a problem whose wording is
    outside the lexicon falls back to industry terms and then to generic
    method terms, which will retrieve adjacent-but-imprecise literature.
    That is the honest failure mode and it is visible in the plan's
    `concepts`.
    """

    def __init__(self, *, queries: int = QUERIES_PER_OPPORTUNITY) -> None:
        self.queries = queries

    def generate(self, context: MarketContext) -> ResearchQueryPlan:
        domain_terms = self._domain_terms(context)
        if not domain_terms:
            # The lexicon recognised nothing and there is no industry to
            # fall back on. Generic queries ("operations research") would
            # buy three real provider runs and return noise, so this
            # fails closed instead -- and the empty concept list is the
            # signal that this context is what an LLM generator is for.
            raise ResearchQueryGenerationError(
                f"no research vocabulary could be derived for signal "
                f"{context.signal_id}; the problem wording matches no known "
                "concept and no industry was supplied"
            )
        candidates = self._candidate_queries(domain_terms)
        selected = select_queries(candidates, limit=self.queries)

        if len(selected) < self.queries:
            raise ResearchQueryGenerationError(
                f"only {len(selected)} distinct research queries could be built "
                f"for signal {context.signal_id}; {self.queries} are required"
            )

        return ResearchQueryPlan(
            signal_id=context.signal_id,
            queries=selected,
            concepts=domain_terms,
            rationale=(
                "Pain vocabulary mapped to research terminology by lexicon: "
                + ", ".join(domain_terms[:4])
                + (
                    f" (industry: {context.industry})"
                    if context.industry
                    else " (no industry supplied)"
                )
            ),
        )

    def _domain_terms(self, context: MarketContext) -> list[str]:
        """Research terms implied by this context, most salient first.

        The title is read before the description because it states the
        problem; the industry is read last so it broadens rather than
        dominates.
        """
        terms: list[str] = []
        seen: set[str] = set()

        def add(term: str) -> None:
            if term not in seen:
                seen.add(term)
                terms.append(term)

        for source in (context.problem, context.description):
            for token in tokenize(source):
                mapped = _DOMAIN_LEXICON.get(token)
                if mapped:
                    add(mapped)

        if context.industry:
            key = context.industry.strip().lower()
            for term in _INDUSTRY_LEXICON.get(key, ()):
                add(term)
            # An unmapped industry is still a real subject word.
            if key not in _INDUSTRY_LEXICON:
                for token in tokenize(context.industry):
                    mapped = _DOMAIN_LEXICON.get(token)
                    add(mapped if mapped else f"{token} systems")

        return terms

    def _candidate_queries(self, domain_terms: list[str]) -> list[str]:
        """Three angles on the same pain, plus fallbacks if terms are thin.

        1. the two strongest domain terms together -- the most specific
           retrieval;
        2. a secondary domain term framed as an optimization problem;
        3. a third domain term framed as a forecasting/allocation problem.

        Distinct angles rather than three phrasings of one, so the three
        provider runs are not paying for the same result set.
        """
        terms = domain_terms

        def term(index: int) -> str:
            return terms[index % len(terms)]

        candidates = [
            f"{term(0)} {term(1)}"
            if len(terms) > 1
            else f"{term(0)} {_METHOD_TERMS[0]}",
            f"{term(1)} {_METHOD_TERMS[0]}",
            f"{term(2)} {_METHOD_TERMS[1]}",
        ]
        # Fallbacks, appended so they are only reached when the three
        # angles above collapse into fewer than three distinct queries.
        candidates.extend(self._fallback_terms(terms))
        return candidates

    def _fallback_terms(self, terms: list[str]) -> list[str]:
        """Extra angles on the strongest term, for a thin but usable context.

        Reached when a context yields only one or two domain terms and
        the three angles above collapse into fewer than three distinct
        queries. One real term framed several ways still retrieves
        different literature; a repeated query would not.
        """
        return [f"{terms[0]} {method}" for method in _METHOD_TERMS[1:]]
