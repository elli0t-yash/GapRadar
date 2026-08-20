"""Turning one provider SERP payload into stable, semantic-free records.

PORTED, NOT IMPORTED, from the R&D prototype at
external/brightdata/investigation/scripts/normalize_serp.py. That tree is
untracked pilot material owned by the acquisition side; importing runtime
code from it would make the backend depend on a collaborator's working
directory, which has already broken this repository once (see
tests/research_intelligence/conftest.py). The behaviour is reproduced
here with tests of its own.

Deliberately semantic-free. It validates shape, normalizes stable fields,
deduplicates canonical URLs within one query and applies the caller's
bound. It never scores, classifies, or drops a result for what it means.
"""

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.web_intelligence.schemas import (
    MAX_QUERY_CHARS,
    MAX_RESULTS_PER_QUERY,
    WebIntelligenceRecord,
)

logger = logging.getLogger(__name__)

# Click-tracking parameters. Stripped because they are per-referral noise:
# two links to the same page differing only by a gclid are one page, and
# keeping them would make the same evidence look like two sources.
#
# Everything NOT on this list is preserved. A query parameter is often
# the whole address -- ?id=, ?p=, ?thread= -- and stripping broadly to
# look tidy would silently collapse distinct pages into one.
_TRACKING_KEYS = frozenset({"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"})
_TRACKING_PREFIXES = ("utm_",)

# Absolute date formats a provider has been observed to emit. Relative
# phrasing ("3 hours ago") is deliberately absent: resolving it needs a
# reference clock nobody recorded, and a guessed date on demand evidence
# would misstate how current a problem is.
_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y")


class WebRecordNormalizationError(ValueError):
    """The caller's own inputs are unusable. Never a provider fault."""


def clean_text(value: Any) -> str:
    """Collapse whitespace; anything that is not a string becomes ""."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(value: Any) -> tuple[str, str] | None:
    """Return (canonical URL, display domain), or None if unusable.

    Canonical means: lowercased scheme and host, IDNA-encoded, default
    port dropped, empty path normalized to "/", fragment removed, and
    tracking parameters stripped while every other parameter is kept in
    its original order.

    Returns None -- rather than raising -- for anything that is not an
    ordinary http(s) address, including credentialed URLs
    (https://user:pass@host). A result GapRadar cannot address is not a
    defect in the batch; it is one row skipped.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        # Embedded credentials in a discovered URL are never something to
        # store, follow, or log.
        if parsed.username or parsed.password:
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None

    scheme = parsed.scheme.lower()
    is_default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = host if port is None or is_default_port else f"{host}:{port}"
    path = parsed.path or "/"

    kept = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]

    canonical = urlunsplit(
        (scheme, netloc, path, urlencode(kept, doseq=True), "")
    )
    return canonical, host.removeprefix("www.")


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_KEYS or lowered.startswith(_TRACKING_PREFIXES)


def normalize_position(result: dict[str, Any]) -> int | None:
    """The provider's rank, or None when it did not give one.

    `global_rank` is the field Bright Data's parsed_light format uses;
    `rank` is accepted too because the fuller parsed format emits it.
    Booleans are refused explicitly -- in Python `True` is an int, and a
    result ranked "True" would otherwise become position 1.
    """
    for field in ("rank", "global_rank"):
        value = result.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def normalize_published_at(result: dict[str, Any]) -> Any:
    """An absolute date the provider stated, or None.

    NOTHING IS INFERRED. A relative phrase, an unparseable string, or a
    missing field all yield None, and a missing date never discards an
    otherwise useful result.
    """
    value = result.get("published_at") or result.get("date")
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None

    try:
        # Python 3.11+ parses a trailing "Z" natively.
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        pass

    for date_format in _DATE_FORMATS:
        try:
            # A calendar DATE is wanted, not an instant: these formats
            # carry no time and no offset, and inventing UTC midnight to
            # satisfy an aware-datetime rule would be exactly the
            # fabrication this function refuses elsewhere.
            return datetime.strptime(candidate, date_format).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def validate_query(query: str) -> str:
    """The submitted query, whitespace-normalized. Raises if unusable."""
    cleaned = clean_text(query)
    if not cleaned:
        raise WebRecordNormalizationError("query must be a non-empty string")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise WebRecordNormalizationError(
            f"query exceeds {MAX_QUERY_CHARS} characters; that is prose, "
            "not a search query"
        )
    return cleaned


def validate_limit(limit: int) -> int:
    """The requested result bound. Raises outside 1..10.

    The ceiling is the one-request contract, not a preference: page 0 of
    a Google SERP carries ten organic results, so eleven would require
    pagination and a second billable request per query.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise WebRecordNormalizationError("limit must be an integer")
    if not 1 <= limit <= MAX_RESULTS_PER_QUERY:
        raise WebRecordNormalizationError(
            f"limit must be between 1 and {MAX_RESULTS_PER_QUERY}: one query "
            "is one page-0 request and page 0 holds ten organic results"
        )
    return limit


def normalize_organic_results(
    payload: Any, *, query: str, limit: int
) -> list[WebIntelligenceRecord]:
    """Map a provider payload's organic array onto the stable contract.

    ONLY `organic` is read. Ads, knowledge panels, top stories and
    "people also ask" are left where they are: they are a different kind
    of claim, and mixing them into evidence would let a paid placement
    become proof that a market exists.

    A payload whose `organic` is missing or is not a list yields [] --
    a SUCCESSFUL empty search. That is the provider's documented shape
    for "nothing matched", and turning it into an error would make a
    quiet topic look like an outage. A transport or status failure never
    reaches this function; the adapter raises before calling it.

    Individual malformed rows are skipped rather than aborting the batch:
    one unusable result among nine usable ones is not a reason to discard
    the search that was already paid for.
    """
    clean_query = validate_query(query)
    bound = validate_limit(limit)

    organic = payload.get("organic") if isinstance(payload, dict) else None
    if not isinstance(organic, list):
        logger.info(
            "web_search_no_organic_array",
            extra={"query_length": len(clean_query)},
        )
        return []

    records: list[WebIntelligenceRecord] = []
    seen_urls: set[str] = set()

    for value in organic:
        if not isinstance(value, dict):
            continue

        title = clean_text(value.get("title"))
        canonical = canonicalize_url(value.get("link") or value.get("url"))
        # A result with no readable title or no addressable URL cannot be
        # shown to anyone or judged against anything.
        if not title or canonical is None:
            continue

        url, domain = canonical
        # WITHIN-QUERY DEDUPE ONLY. The first occurrence wins, so the
        # better-ranked copy is the one kept. Cross-query duplicates are
        # deliberately NOT removed here -- each query that found a URL is
        # provenance, and how many search directions converged on a page
        # is one of the few honest strength signals discovery produces.
        if url in seen_urls:
            continue
        seen_urls.add(url)

        records.append(
            WebIntelligenceRecord(
                query=clean_query,
                title=title,
                url=url,
                domain=domain,
                snippet=clean_text(value.get("description") or value.get("snippet")),
                position=normalize_position(value),
                published_at=normalize_published_at(value),
            )
        )
        if len(records) == bound:
            break

    return records
