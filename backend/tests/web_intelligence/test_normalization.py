"""Provider normalization: shape only, never meaning.

Ports the R&D prototype's regression suite into the backend. The
prototype is untracked pilot material owned by the acquisition side, so
these tests exist here rather than importing anything from it.

THE CENTRAL DISTINCTION, exercised throughout: a payload with no organic
results is a SUCCESSFUL empty search. Provider failure never reaches this
module -- the adapter raises first.
"""

from datetime import date

import pytest

from app.web_intelligence.normalization import (
    WebRecordNormalizationError,
    canonicalize_url,
    clean_text,
    normalize_organic_results,
    normalize_position,
    validate_limit,
    validate_query,
)
from app.web_intelligence.schemas import MAX_QUERY_CHARS, MAX_RESULTS_PER_QUERY

QUERY = "restaurant inventory waste problems"


def organic(**overrides: object) -> dict[str, object]:
    row = {
        "title": "Food Inventory Management: Cut Waste",
        "link": "https://buyersedgeplatform.com/blog/food-inventory-management/",
        "description": "Spoilage and overordering cause inventory loss.",
        "global_rank": 2,
    }
    row.update(overrides)
    return row


def payload(*rows: dict[str, object]) -> dict[str, object]:
    return {"organic": list(rows)}


# -- the record contract ----------------------------------------------------


def test_a_usable_row_becomes_a_record() -> None:
    [record] = normalize_organic_results(payload(organic()), query=QUERY, limit=10)

    assert record.query == QUERY
    assert record.title == "Food Inventory Management: Cut Waste"
    assert record.url == (
        "https://buyersedgeplatform.com/blog/food-inventory-management/"
    )
    assert record.domain == "buyersedgeplatform.com"
    assert record.snippet == "Spoilage and overordering cause inventory loss."
    assert record.position == 2
    assert record.published_at is None


def test_the_record_carries_no_semantic_fields() -> None:
    """Acquisition that classifies is acquisition that cannot be audited."""
    [record] = normalize_organic_results(payload(organic()), query=QUERY, limit=10)

    forbidden = {"is_competitor", "pain_strength", "relevance_score", "sentiment"}
    assert not (set(record.model_dump()) & forbidden)


def test_whitespace_is_collapsed_in_text_fields() -> None:
    [record] = normalize_organic_results(
        payload(organic(title="  Cut   Waste \n Now  ")), query=QUERY, limit=10
    )
    assert record.title == "Cut Waste Now"


def test_clean_text_of_a_non_string_is_empty() -> None:
    assert clean_text(None) == ""
    assert clean_text(7) == ""


# -- query and limit --------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_a_blank_query_is_refused(query: str) -> None:
    with pytest.raises(WebRecordNormalizationError):
        validate_query(query)


def test_an_enormous_query_is_refused() -> None:
    """That is prose, not a search query."""
    with pytest.raises(WebRecordNormalizationError):
        validate_query("x" * (MAX_QUERY_CHARS + 1))


def test_the_query_is_whitespace_normalized() -> None:
    assert validate_query("  a   query  ") == "a query"


@pytest.mark.parametrize("limit", [1, 5, MAX_RESULTS_PER_QUERY])
def test_a_limit_within_one_page_is_accepted(limit: int) -> None:
    assert validate_limit(limit) == limit


@pytest.mark.parametrize("limit", [0, -1, MAX_RESULTS_PER_QUERY + 1, 100])
def test_a_limit_outside_one_page_is_refused(limit: int) -> None:
    """Eleven would need pagination: a second billable request per query."""
    with pytest.raises(WebRecordNormalizationError):
        validate_limit(limit)


def test_a_boolean_limit_is_refused() -> None:
    """In Python True is an int; a "limit" of True must not become 1."""
    with pytest.raises(WebRecordNormalizationError):
        validate_limit(True)


def test_the_limit_bounds_the_returned_records() -> None:
    rows = [organic(link=f"https://a.test/{index}") for index in range(10)]
    records = normalize_organic_results(payload(*rows), query=QUERY, limit=3)
    assert len(records) == 3


# -- URL canonicalization ---------------------------------------------------


def test_scheme_and_host_are_lowercased() -> None:
    assert canonicalize_url("HTTPS://WWW.Example.COM/Path") == (
        "https://www.example.com/Path",
        "example.com",
    )


def test_the_path_case_is_preserved() -> None:
    """A path is often case-sensitive; lowercasing it would 404."""
    url, _ = canonicalize_url("https://example.com/CaseSensitive/ID")
    assert url == "https://example.com/CaseSensitive/ID"


def test_an_empty_path_becomes_a_slash() -> None:
    url, _ = canonicalize_url("https://example.com")
    assert url == "https://example.com/"


def test_the_fragment_is_removed() -> None:
    url, _ = canonicalize_url("https://example.com/a#section-3")
    assert url == "https://example.com/a"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
    ],
)
def test_the_default_port_is_dropped(raw: str, expected: str) -> None:
    url, _ = canonicalize_url(raw)
    assert url == expected


def test_a_non_default_port_is_kept() -> None:
    url, _ = canonicalize_url("https://example.com:8443/a")
    assert url == "https://example.com:8443/a"


def test_the_domain_drops_a_leading_www() -> None:
    _, domain = canonicalize_url("https://www.example.com/a")
    assert domain == "example.com"


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/a",
        "javascript:alert(1)",
        "/relative/path",
        "",
        "   ",
        "https:///nohost",
    ],
)
def test_an_unusable_url_is_refused(raw: str) -> None:
    assert canonicalize_url(raw) is None


def test_a_credentialed_url_is_refused() -> None:
    """Embedded credentials are never something to store, follow or log."""
    assert canonicalize_url("https://user:secret@example.com/a") is None


def test_a_non_string_url_is_refused() -> None:
    assert canonicalize_url(None) is None
    assert canonicalize_url(42) is None


# -- tracking parameters ----------------------------------------------------


@pytest.mark.parametrize(
    "tracker",
    [
        "utm_source=newsletter",
        "utm_medium=email",
        "utm_campaign=q3",
        "UTM_SOURCE=shouty",
        "fbclid=abc123",
        "gclid=def456",
        "msclkid=ghi789",
    ],
)
def test_tracking_parameters_are_removed(tracker: str) -> None:
    url, _ = canonicalize_url(f"https://example.com/a?{tracker}")
    assert url == "https://example.com/a"


def test_meaningful_parameters_are_preserved() -> None:
    """A query parameter is often the whole address."""
    url, _ = canonicalize_url("https://example.com/thread?id=17&p=3&sort=new")
    assert url == "https://example.com/thread?id=17&p=3&sort=new"


def test_meaningful_parameters_survive_alongside_trackers() -> None:
    url, _ = canonicalize_url(
        "https://example.com/thread?id=17&utm_source=x&gclid=y&p=3"
    )
    assert url == "https://example.com/thread?id=17&p=3"


def test_a_blank_valued_parameter_is_preserved() -> None:
    url, _ = canonicalize_url("https://example.com/a?flag=")
    assert url == "https://example.com/a?flag="


# -- deduplication ----------------------------------------------------------


def test_duplicate_urls_within_one_query_are_removed() -> None:
    records = normalize_organic_results(
        payload(
            organic(link="https://a.test/x", title="First"),
            organic(link="https://a.test/x?utm_source=q", title="Second"),
            organic(link="https://a.test/x#frag", title="Third"),
        ),
        query=QUERY,
        limit=10,
    )

    assert len(records) == 1
    # The FIRST occurrence wins, so the better-ranked copy is kept.
    assert records[0].title == "First"


def test_different_urls_are_not_deduplicated() -> None:
    records = normalize_organic_results(
        payload(
            organic(link="https://a.test/x"),
            organic(link="https://a.test/y"),
        ),
        query=QUERY,
        limit=10,
    )
    assert len(records) == 2


def test_cross_query_duplicates_are_not_this_layers_concern() -> None:
    """Each query that found a URL is provenance, kept deliberately.

    Normalization sees one query at a time; the same page returned by two
    queries produces two records, each carrying its own `query`.
    """
    first = normalize_organic_results(
        payload(organic(link="https://a.test/x")), query="demand query", limit=10
    )
    second = normalize_organic_results(
        payload(organic(link="https://a.test/x")), query="competitor query", limit=10
    )

    assert first[0].url == second[0].url
    assert {first[0].query, second[0].query} == {"demand query", "competitor query"}


# -- malformed and absent data ----------------------------------------------


def test_a_row_with_no_title_is_skipped() -> None:
    records = normalize_organic_results(
        payload(organic(title=""), organic(link="https://b.test/")),
        query=QUERY,
        limit=10,
    )
    assert len(records) == 1


def test_a_row_with_an_unusable_url_is_skipped() -> None:
    records = normalize_organic_results(
        payload(organic(link="not-a-url"), organic(link="https://b.test/")),
        query=QUERY,
        limit=10,
    )
    assert len(records) == 1


def test_a_non_object_row_is_skipped_without_aborting_the_batch() -> None:
    """One unusable row is not a reason to discard a paid-for search."""
    records = normalize_organic_results(
        {"organic": ["nonsense", None, 7, organic()]}, query=QUERY, limit=10
    )
    assert len(records) == 1


@pytest.mark.parametrize(
    "body",
    [{}, {"organic": None}, {"organic": "not a list"}, {"organic": []}],
)
def test_an_absent_or_malformed_organic_array_is_a_successful_empty_search(
    body: dict[str, object],
) -> None:
    """The provider's documented shape for "nothing matched".

    Turning this into an error would make a quiet topic look like an
    outage -- the exact confusion this pipeline is built to avoid.
    """
    assert normalize_organic_results(body, query=QUERY, limit=10) == []


def test_a_non_dict_payload_is_a_successful_empty_search() -> None:
    assert normalize_organic_results(["unexpected"], query=QUERY, limit=10) == []


# -- position and date ------------------------------------------------------


def test_rank_is_preferred_over_global_rank() -> None:
    assert normalize_position({"rank": 3, "global_rank": 9}) == 3


def test_a_numeric_string_rank_is_accepted() -> None:
    assert normalize_position({"global_rank": "4"}) == 4


def test_a_boolean_rank_is_refused() -> None:
    """True is an int in Python; a result ranked True is not position 1."""
    assert normalize_position({"rank": True, "global_rank": 5}) == 5


def test_a_missing_rank_is_none_not_zero() -> None:
    """Zero would be a real first position."""
    assert normalize_position({}) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-04", date(2026, 3, 4)),
        ("Mar 04, 2026", date(2026, 3, 4)),
        ("March 04, 2026", date(2026, 3, 4)),
    ],
)
def test_an_absolute_date_is_parsed(raw: str, expected: date) -> None:
    [record] = normalize_organic_results(
        payload(organic(date=raw)), query=QUERY, limit=10
    )
    assert record.published_at == expected


@pytest.mark.parametrize("raw", ["3 hours ago", "yesterday", "", "sometime"])
def test_a_relative_or_unparseable_date_becomes_none(raw: str) -> None:
    """NOTHING IS INFERRED. A guessed date misstates how current a problem is."""
    [record] = normalize_organic_results(
        payload(organic(date=raw)), query=QUERY, limit=10
    )
    assert record.published_at is None


def test_a_missing_date_never_discards_a_result() -> None:
    [record] = normalize_organic_results(payload(organic()), query=QUERY, limit=10)
    assert record.published_at is None
    assert record.url
