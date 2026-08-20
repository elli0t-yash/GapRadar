"""The provider-neutral contracts for web evidence acquisition.

DELIBERATELY SEMANTIC-FREE. Nothing in this module scores, classifies,
ranks by meaning, or decides whether a page is a competitor. It describes
what a search engine returned and nothing more. Semantics live in
app.web_intelligence.classification, one layer up, so a provider swap can
never quietly change a judgement.

Kept apart from app.research_intelligence.schemas on purpose. A research
paper is an entity with an identity (arxiv_id), an abstract and authors;
a web result is one observation of a URL at one rank for one query. They
share no fields worth sharing, and a common base class would exist only
to make the two look related.
"""

import enum
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

# The one-request contract, proven in the Bright Data pilot: one query is
# one SERP request for page 0, and Google's first page carries ten
# organic results. A higher bound would silently require pagination --
# a second billable request per query -- so it is refused rather than
# quietly honoured.
MAX_RESULTS_PER_QUERY = 10

# Longest query GapRadar will submit. Search engines truncate far beyond
# this, and a longer string is a sign the planner emitted prose rather
# than a query.
MAX_QUERY_CHARS = 256


class WebSearchFamily(str, enum.Enum):
    """WHY a web search was run.

    Not a semantic verdict about what came back -- it is the question the
    search was asked, decided before the provider was contacted. It is
    what lets "3 of 3 demand searches complete" be a fact rather than a
    guess, and what keeps demand provenance separate from competitor
    provenance when the same URL is found by both.
    """

    DEMAND = "demand"
    COMPETITOR = "competitor"


class SearchLocale(BaseModel):
    """Where and in what language a search is executed.

    RETRIEVAL CONFIGURATION, NEVER EVIDENCE. A locale changes which
    results a search engine returns; it says nothing about the market, the
    user, or the opportunity, and nothing downstream may read it as if it
    did. It is stored alongside the provider execution record rather than
    on any evidence row for exactly that reason.

    Deliberately not inferred from a user's IP, browser, or physical
    location. GapRadar knows what an investigation says about itself and
    nothing else.
    """

    model_config = ConfigDict(frozen=True)

    # ISO 3166-1 alpha-2, lowercase -- what Google's `gl` parameter takes.
    country: str = Field(min_length=2, max_length=2, pattern="^[a-z]{2}$")
    # ISO 639-1, lowercase -- Google's `hl`.
    language: str = Field(min_length=2, max_length=2, pattern="^[a-z]{2}$")

    def __str__(self) -> str:  # pragma: no cover - logging aid
        return f"{self.country}/{self.language}"


# US English. Chosen, not inferred: the pilot ran every query this way,
# the SERP corpus for product and problem language is largest in it, and
# a default that follows whoever is deployed would make two identical
# investigations return different evidence.
DEFAULT_LOCALE = SearchLocale(country="us", language="en")

# The one exception the pilot justified. Its India-specific query returned
# nothing usable under us/en -- the provider rejected a redirect -- and
# returned 8 organic results under in/en. So an investigation that says it
# is about India is searched from India.
INDIA_LOCALE = SearchLocale(country="in", language="en")


class WebIntelligenceRecord(BaseModel):
    """One organic search result, normalized. No meaning attached.

    Every field is either what the provider returned or a deterministic
    normalization of it. There is deliberately no `is_competitor`, no
    `pain_strength`, no `relevance_score` and no sentiment: acquisition
    that classifies is acquisition that cannot be audited, because a
    disagreement about a verdict becomes indistinguishable from a
    disagreement about what was retrieved.

    `query` is carried on the record rather than only on the batch, so a
    record remains self-describing once results from several searches are
    merged -- which is exactly what the demand and competitor phases do.
    """

    model_config = ConfigDict(frozen=True)

    # The query as submitted, whitespace-normalized. Provenance: the same
    # URL found by two queries yields two records, and that is the point.
    query: str
    title: str
    # Canonical form: lowercased scheme/host, default port and fragment
    # removed, tracking parameters stripped. This is the identity a piece
    # of evidence is deduplicated on.
    url: str
    # Lowercased registrable host without a leading "www.".
    domain: str
    snippet: str = ""
    # The provider's rank on page 0. None when it did not supply one --
    # never 0, which would be a real first position.
    position: int | None = None
    # ONLY when the provider stated a reliable absolute date. Never
    # inferred from the URL, the snippet, or a relative phrase like
    # "3 hours ago": a wrong date on demand evidence would misrepresent
    # how current the problem is.
    published_at: date | None = None
