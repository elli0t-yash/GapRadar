"""What the arXiv normalizer accepts, what it refuses, and what it fixes.

The contract under test is external/brightdata/arxiv/schema.json. The
first test runs the real committed collector output through the
normalizer, so a change to either side that breaks the handoff fails
here rather than in production.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.research_intelligence.normalizer import (
    ResearchRecordRejectedError,
    normalize_arxiv_record,
)
from app.research_intelligence.schemas import ResearchRejectionReason
from tests.research_intelligence.conftest import arxiv_record, arxiv_record_for


def reason_of(record: dict[str, Any]) -> ResearchRejectionReason:
    with pytest.raises(ResearchRecordRejectedError) as excinfo:
        normalize_arxiv_record(record)
    return excinfo.value.reason


# -- the real contract ------------------------------------------------------


def test_every_validated_collector_record_normalizes(
    arxiv_records: list[dict[str, Any]],
) -> None:
    """Real validated collector records must all survive ingestion.

    This is the handoff contract itself: if the collector's output shape
    and this normalizer ever disagree, that is the failure to catch.
    """
    papers = [normalize_arxiv_record(record) for record in arxiv_records]

    assert len(papers) == len(arxiv_records)
    assert len({paper.arxiv_id for paper in papers}) == len(arxiv_records)
    assert all(paper.title and paper.abstract for paper in papers)
    assert all(paper.authors for paper in papers)
    assert all(paper.categories for paper in papers)
    assert all(isinstance(paper.published_at, date) for paper in papers)
    assert all(paper.paper_url.startswith("https://arxiv.org/abs/") for paper in papers)
    assert all(paper.pdf_url.startswith("https://arxiv.org/pdf/") for paper in papers)


def test_a_valid_record_normalizes_field_by_field() -> None:
    paper = normalize_arxiv_record(arxiv_record())

    assert paper.arxiv_id == "2608.13083"
    assert paper.title.startswith("AoI-Guaranteed")
    assert paper.authors == ["Sajedeh Norouzi", "Maryam Ansarifard"]
    assert paper.published_at == date(2026, 8, 13)
    assert paper.paper_url == "https://arxiv.org/abs/2608.13083"
    assert paper.pdf_url == "https://arxiv.org/pdf/2608.13083"


def test_the_records_query_is_not_part_of_the_paper() -> None:
    """A paper is an entity; the query that found it is search provenance.

    The collector currently pins its own `query` field, so trusting it
    would mislabel every future dynamic query.
    """
    paper = normalize_arxiv_record(arxiv_record(query="something else entirely"))

    assert not hasattr(paper, "query")
    assert "query" not in paper.model_dump()


def test_bright_datas_input_provenance_object_is_ignored() -> None:
    """The platform adds `input`; it is run provenance, not paper data."""
    record = arxiv_record(input={"url": "https://arxiv.org/search/?query=x"})

    assert normalize_arxiv_record(record).arxiv_id == "2608.13083"


# -- arXiv identifiers ------------------------------------------------------


@pytest.mark.parametrize(
    "arxiv_id",
    [
        "2608.1308",  # 4-digit modern form
        "2608.13083",  # 5-digit modern form
        "math.OC/0123456",  # legacy archive/number form
    ],
)
def test_both_arxiv_identifier_forms_are_accepted(arxiv_id: str) -> None:
    assert normalize_arxiv_record(arxiv_record_for(arxiv_id)).arxiv_id == arxiv_id


@pytest.mark.parametrize("suffix", ["v1", "v2", "V11"])
def test_a_version_suffix_is_stripped_not_rejected(suffix: str) -> None:
    """v1 and v3 are revisions of one paper, not two papers."""
    record = arxiv_record(
        arxiv_id=f"2608.13083{suffix}",
        paper_url=f"https://arxiv.org/abs/2608.13083{suffix}",
        pdf_url=f"https://arxiv.org/pdf/2608.13083{suffix}",
    )

    assert normalize_arxiv_record(record).arxiv_id == "2608.13083"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not-an-id",
        "26081308",  # no separator
        "2608.130834",  # six digits: outside the published 4-5 digit form
        None,
        2608.13083,  # a float, not a string
    ],
)
def test_an_unrecognizable_arxiv_id_is_rejected(bad: Any) -> None:
    assert reason_of(arxiv_record(arxiv_id=bad)) in {
        ResearchRejectionReason.MISSING_REQUIRED_FIELD,
        ResearchRejectionReason.INVALID_ARXIV_ID,
    }


# -- required fields --------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["arxiv_id", "title", "abstract", "authors", "published_at", "categories"]
)
def test_a_missing_required_field_is_rejected(field: str) -> None:
    record = arxiv_record()
    del record[field]

    assert reason_of(record) is not None


@pytest.mark.parametrize("field", ["title", "abstract"])
def test_a_blank_text_field_is_rejected(field: str) -> None:
    assert (
        reason_of(arxiv_record(**{field: "   "}))
        is ResearchRejectionReason.MISSING_REQUIRED_FIELD
    )


def test_whitespace_is_normalized_without_changing_meaning() -> None:
    paper = normalize_arxiv_record(
        arxiv_record(
            title="  Dynamic   Route\t\tPlanning  ",
            abstract="  A   novel  approach.  ",
            authors=["  Sajedeh   Norouzi  "],
        )
    )

    assert paper.title == "Dynamic Route Planning"
    assert paper.abstract == "A novel approach."
    assert paper.authors == ["Sajedeh Norouzi"]


# -- authors ----------------------------------------------------------------


def test_author_order_is_preserved() -> None:
    authors = ["Third Author", "First Author", "Second Author"]

    assert normalize_arxiv_record(arxiv_record(authors=authors)).authors == authors


def test_repeated_authors_are_collapsed() -> None:
    """A scraping artifact, removed. A distinct name is never dropped."""
    record = arxiv_record(authors=["Ada Lovelace", "Alan Turing", "Ada Lovelace"])

    assert normalize_arxiv_record(record).authors == ["Ada Lovelace", "Alan Turing"]


@pytest.mark.parametrize(
    "authors", [[], "Ada Lovelace", [""], ["  "], ["Ada", 42], [None]]
)
def test_a_malformed_author_list_is_rejected(authors: Any) -> None:
    assert reason_of(arxiv_record(authors=authors)) in {
        ResearchRejectionReason.INVALID_AUTHORS,
        ResearchRejectionReason.MISSING_REQUIRED_FIELD,
    }


# -- categories -------------------------------------------------------------


def test_a_category_is_split_into_code_and_label() -> None:
    paper = normalize_arxiv_record(
        arxiv_record(categories=["Systems and Control (eess.SY)"])
    )

    assert paper.categories[0].code == "eess.SY"
    assert paper.categories[0].label == "Systems and Control"
    assert paper.primary_category_code == "eess.SY"


def test_a_hyphenated_category_code_is_parsed() -> None:
    """quant-ph is a real arXiv code and has no dot."""
    paper = normalize_arxiv_record(
        arxiv_record(categories=["Quantum Physics (quant-ph)"])
    )

    assert paper.categories[0].code == "quant-ph"


def test_multiple_categories_keep_their_order_and_the_first_is_primary() -> None:
    paper = normalize_arxiv_record(
        arxiv_record(
            categories=[
                "Machine Learning (cs.LG)",
                "Optimization and Control (math.OC)",
            ]
        )
    )

    assert [c.code for c in paper.categories] == ["cs.LG", "math.OC"]
    assert paper.primary_category_code == "cs.LG"


def test_a_bare_category_code_is_accepted() -> None:
    paper = normalize_arxiv_record(arxiv_record(categories=["eess.SY"]))

    assert paper.categories[0].code == "eess.SY"
    assert paper.categories[0].label == "eess.SY"


def test_an_unparseable_category_is_kept_as_a_label_not_discarded() -> None:
    """The source owns its vocabulary; an unfamiliar shape is not a defect."""
    paper = normalize_arxiv_record(arxiv_record(categories=["Some New Subject Area"]))

    assert paper.categories[0].code is None
    assert paper.categories[0].label == "Some New Subject Area"
    assert paper.primary_category_code is None


def test_repeated_categories_are_collapsed() -> None:
    record = arxiv_record(categories=["Robotics (cs.RO)", "Robotics (cs.RO)"])

    assert len(normalize_arxiv_record(record).categories) == 1


@pytest.mark.parametrize("categories", [[], "cs.LG", [""], [42], [None]])
def test_a_malformed_category_list_is_rejected(categories: Any) -> None:
    assert reason_of(arxiv_record(categories=categories)) in {
        ResearchRejectionReason.INVALID_CATEGORIES,
        ResearchRejectionReason.MISSING_REQUIRED_FIELD,
    }


# -- publication date -------------------------------------------------------


def test_an_iso_date_string_is_parsed_as_a_calendar_date() -> None:
    paper = normalize_arxiv_record(arxiv_record(published_at="2026-08-13"))

    assert paper.published_at == date(2026, 8, 13)
    assert not isinstance(paper.published_at, datetime)


def test_a_date_object_passes_through() -> None:
    assert normalize_arxiv_record(
        arxiv_record(published_at=date(2026, 8, 13))
    ).published_at == date(2026, 8, 13)


def test_a_datetime_is_rejected_rather_than_truncated() -> None:
    """Taking .date() would require guessing a timezone. This one does not."""
    assert (
        reason_of(arxiv_record(published_at=datetime(2026, 8, 13, 10, 30, tzinfo=UTC)))
        is ResearchRejectionReason.INVALID_PUBLICATION_DATE
    )


@pytest.mark.parametrize(
    "bad", ["2026-13-45", "13/08/2026", "2026-08-13T10:00:00", "yesterday", 20260813]
)
def test_an_unparseable_publication_date_is_rejected(bad: Any) -> None:
    assert (
        reason_of(arxiv_record(published_at=bad))
        is ResearchRejectionReason.INVALID_PUBLICATION_DATE
    )


# -- URLs -------------------------------------------------------------------


def test_urls_are_normalized() -> None:
    paper = normalize_arxiv_record(
        arxiv_record(
            paper_url="https://ARXIV.org/abs/2608.13083/#section",
            pdf_url="https://arxiv.org/pdf/2608.13083",
        )
    )

    assert paper.paper_url == "https://arxiv.org/abs/2608.13083"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("paper_url", "https://evil.example.com/abs/2608.13083"),
        ("paper_url", "http://arxiv.org/abs/2608.13083"),
        ("paper_url", "https://arxiv.org/pdf/2608.13083"),
        ("paper_url", "not a url"),
        ("paper_url", ""),
        ("pdf_url", "https://evil.example.com/pdf/2608.13083"),
        ("pdf_url", "https://arxiv.org/abs/2608.13083"),
        ("pdf_url", None),
    ],
)
def test_a_url_that_is_not_the_expected_arxiv_url_is_rejected(
    field: str, bad: Any
) -> None:
    assert reason_of(arxiv_record(**{field: bad})) in {
        ResearchRejectionReason.INVALID_URL,
        ResearchRejectionReason.MISSING_REQUIRED_FIELD,
    }


def test_a_url_naming_a_different_paper_is_rejected() -> None:
    """Row misalignment: every field is well-formed, but they disagree.

    Invisible field by field, and undetectable once persisted.
    """
    record = arxiv_record(
        arxiv_id="2608.13083", paper_url="https://arxiv.org/abs/2607.22582"
    )

    assert reason_of(record) is ResearchRejectionReason.INVALID_URL


def test_a_versioned_url_still_identifies_the_unversioned_id() -> None:
    record = arxiv_record(
        arxiv_id="2608.13083",
        paper_url="https://arxiv.org/abs/2608.13083v2",
        pdf_url="https://arxiv.org/pdf/2608.13083v2",
    )

    assert normalize_arxiv_record(record).arxiv_id == "2608.13083"


def test_a_legacy_identifier_url_is_matched_correctly() -> None:
    """The legacy id contains a slash, which naive URL splitting breaks."""
    paper = normalize_arxiv_record(arxiv_record_for("math.OC/0123456"))

    assert paper.arxiv_id == "math.OC/0123456"
    assert paper.paper_url == "https://arxiv.org/abs/math.OC/0123456"


# -- record shape -----------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "a string", 42, ["a", "list"]])
def test_a_non_object_record_is_rejected(bad: Any) -> None:
    with pytest.raises(ResearchRecordRejectedError) as excinfo:
        normalize_arxiv_record(bad)

    assert excinfo.value.reason is ResearchRejectionReason.INVALID_RECORD
