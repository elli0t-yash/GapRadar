"""Turn one untrusted arXiv record into a validated, normalized paper.

Pure functions over a dict. No database, no network, no provider client.
Every failure raises ResearchRecordRejectedError with a stable reason
code; nothing is ever invented to fill an absent field.

The whitespace and URL primitives are imported from
app.ingestion.normalizer rather than reimplemented, so the market side
and the research side cannot drift into two different ideas of what
"normalized text" means.
"""

import re
from datetime import date, datetime
from typing import Any

from app.ingestion.normalizer import normalize_text, normalize_url
from app.research_intelligence.schemas import (
    NormalizedResearchPaper,
    RawResearchRecord,
    ResearchCategory,
    ResearchRejectionReason,
)

# Column length limits, mirrored from app/db/models/research_paper.py and
# checked here so an oversized value is rejected with a reason code
# instead of surfacing later as a DB-level DataError.
_ARXIV_ID_MAX_LEN = 64
_TITLE_MAX_LEN = 1024
_URL_MAX_LEN = 2048

# Both arXiv identifier forms, from the collector's own schema.json:
# modern ("2608.13083") and legacy ("math.OC/0123456").
_ARXIV_ID_RE = re.compile(r"^(?:[0-9]{4}\.[0-9]{4,5}|[A-Za-z.-]+/[0-9]{7})$")
# A trailing revision marker: "2608.13083v2". Stripped, never rejected --
# v1 and v3 are revisions of one paper, not two papers.
_ARXIV_VERSION_SUFFIX_RE = re.compile(r"v[0-9]+$", re.IGNORECASE)
# "Systems and Control (eess.SY)" -> label + code.
_CATEGORY_RE = re.compile(r"^(?P<label>.+?)\s*\((?P<code>[A-Za-z][A-Za-z0-9.\-]*)\)$")
# A bare category code with no label, e.g. "eess.SY" or "quant-ph".
_BARE_CATEGORY_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]*$")

_ARXIV_HOST = "arxiv.org"
_ABS_PATH_PREFIX = "/abs/"
_PDF_PATH_PREFIX = "/pdf/"


class ResearchRecordRejectedError(Exception):
    """Raised when a raw record fails validation.

    Caught by app.research_intelligence.service and turned into a
    RejectedResearchRecord; it never escapes an ingestion call.
    """

    def __init__(self, reason: ResearchRejectionReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


def normalize_arxiv_id(value: Any) -> str:
    """Validate an arXiv identifier and strip any version suffix.

    Case is preserved: legacy identifiers carry a case-sensitive archive
    prefix ("math.OC/0123456"), so lowercasing would invent a different
    id.
    """
    if not isinstance(value, str) or not value.strip():
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, "arxiv_id is required"
        )

    candidate = value.strip()
    # Strip the version before validating, so "2608.13083v2" is accepted
    # and resolves to the same identity as "2608.13083".
    stripped = _ARXIV_VERSION_SUFFIX_RE.sub("", candidate)
    if not _ARXIV_ID_RE.match(stripped):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_ARXIV_ID,
            f"not a recognizable arXiv id: {value!r}",
        )
    if len(stripped) > _ARXIV_ID_MAX_LEN:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_ARXIV_ID,
            f"arxiv_id exceeds {_ARXIV_ID_MAX_LEN} characters",
        )
    return stripped


def normalize_authors(value: Any) -> list[str]:
    """Validate and normalize the author list.

    Requires a non-empty list of non-blank strings. Order is preserved --
    author order is meaningful on a paper -- and exact repeats are
    collapsed, which removes a scraping artifact without dropping a real
    distinct name.
    """
    if value is None:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, "authors is required"
        )
    if not isinstance(value, list):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_AUTHORS,
            f"authors must be a list, got {type(value).__name__}",
        )

    authors: list[str] = []
    seen: set[str] = set()
    for index, raw_author in enumerate(value):
        if not isinstance(raw_author, str):
            raise ResearchRecordRejectedError(
                ResearchRejectionReason.INVALID_AUTHORS,
                f"author at index {index} is not a string",
            )
        author = normalize_text(raw_author)
        if not author:
            raise ResearchRecordRejectedError(
                ResearchRejectionReason.INVALID_AUTHORS,
                f"author at index {index} is blank",
            )
        if author in seen:
            continue
        seen.add(author)
        authors.append(author)

    if not authors:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_AUTHORS, "authors must not be empty"
        )
    return authors


def normalize_categories(value: Any) -> list[ResearchCategory]:
    """Validate the category list and split each entry into code + label.

    Three shapes are handled, in order:

    1. "Systems and Control (eess.SY)" -> label and code.
    2. "eess.SY"                       -> code only; the label repeats it,
                                          because there is no other text.
    3. anything else non-blank         -> label only, code None. The
                                          source owns its vocabulary; an
                                          unparseable category is kept as
                                          evidence rather than dropped.

    A non-list, or a list containing a non-string or a blank, is
    rejected -- that is a broken payload, not an unfamiliar vocabulary.
    """
    if value is None:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, "categories is required"
        )
    if not isinstance(value, list):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_CATEGORIES,
            f"categories must be a list, got {type(value).__name__}",
        )

    categories: list[ResearchCategory] = []
    seen: set[tuple[str | None, str]] = set()
    for index, raw_category in enumerate(value):
        if not isinstance(raw_category, str):
            raise ResearchRecordRejectedError(
                ResearchRejectionReason.INVALID_CATEGORIES,
                f"category at index {index} is not a string",
            )
        text = normalize_text(raw_category)
        if not text:
            raise ResearchRecordRejectedError(
                ResearchRejectionReason.INVALID_CATEGORIES,
                f"category at index {index} is blank",
            )

        match = _CATEGORY_RE.match(text)
        if match:
            category = ResearchCategory(
                code=match.group("code"), label=normalize_text(match.group("label"))
            )
        elif _BARE_CATEGORY_CODE_RE.match(text):
            category = ResearchCategory(code=text, label=text)
        else:
            category = ResearchCategory(code=None, label=text)

        key = (category.code, category.label)
        if key in seen:
            continue
        seen.add(key)
        categories.append(category)

    if not categories:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_CATEGORIES, "categories must not be empty"
        )
    return categories


def parse_publication_date(value: Any) -> date:
    """Parse arXiv's publication date as a calendar DATE.

    arXiv publishes "2026-08-13": no time, no timezone. A datetime is
    rejected rather than truncated, because deciding which timezone to
    take `.date()` in would be a guess -- the same refusal
    app.ingestion.normalizer.parse_timestamp makes about naive
    timestamps, from the other direction.
    """
    if value is None:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, "published_at is required"
        )
    # datetime is a subclass of date, so this check must come first.
    if isinstance(value, datetime):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_PUBLICATION_DATE,
            "published_at must be a calendar date, not a datetime; converting "
            "one would require guessing a timezone",
        )
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_PUBLICATION_DATE,
            f"unsupported published_at type: {type(value).__name__}",
        )

    text = value.strip()
    if not text:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, "published_at is blank"
        )
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_PUBLICATION_DATE,
            f"could not parse published_at as a date: {value!r}",
        ) from exc


def normalize_arxiv_url(value: Any, *, field: str, path_prefix: str) -> str:
    """Validate and normalize one arXiv URL.

    Requires https on arxiv.org under the expected path prefix. A URL
    pointing somewhere else is rejected rather than followed: these
    values are rendered as links and handed to users, so an off-host URL
    in a research payload is exactly the thing not to pass through.
    """
    if not isinstance(value, str) or not value.strip():
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, f"{field} is required"
        )
    try:
        url = normalize_url(value)
    except ValueError as exc:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_URL, f"{field}: {exc}"
        ) from exc

    if not url.startswith("https://"):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_URL, f"{field} must use https"
        )
    if not url.startswith(f"https://{_ARXIV_HOST}{path_prefix}"):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_URL,
            f"{field} must be an https://{_ARXIV_HOST}{path_prefix} URL, got {value!r}",
        )
    if len(url) > _URL_MAX_LEN:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_URL,
            f"{field} exceeds {_URL_MAX_LEN} characters",
        )
    return url


def _require_url_identifies(
    url: str, *, arxiv_id: str, field: str, path_prefix: str
) -> None:
    """The URL must name the same paper as arxiv_id.

    A mismatch means the scraper paired one paper's title with another
    paper's link -- row misalignment, which is invisible field by field
    because every value is individually well-formed. Cheap to catch here
    and effectively undetectable downstream.

    The tail is taken by stripping the prefix the URL was already
    validated against, rather than by splitting on "/", because a legacy
    identifier contains a slash of its own ("math.OC/0123456").
    """
    tail = url[len(f"https://{_ARXIV_HOST}{path_prefix}") :]
    if _ARXIV_VERSION_SUFFIX_RE.sub("", tail) != arxiv_id:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_URL,
            f"{field} does not identify arxiv_id {arxiv_id!r}: {url!r}",
        )


def _require_text(value: Any, *, field: str, max_len: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD, f"{field} is required"
        )
    text = normalize_text(value)
    if not text:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.MISSING_REQUIRED_FIELD,
            f"{field} is blank after normalization",
        )
    if max_len is not None and len(text) > max_len:
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_RECORD,
            f"{field} exceeds {max_len} characters",
        )
    return text


def normalize_arxiv_record(raw: RawResearchRecord) -> NormalizedResearchPaper:
    """Validate and normalize one raw arXiv record.

    `query` and Bright Data's platform-added `input` object are read by
    nothing here, on purpose. `query` is provenance about a SEARCH, and
    the same paper is legitimately returned by many searches -- it is
    recorded on ResearchSearchRun instead. The authoritative query comes
    from the ingestion caller, not from the record, because the collector
    currently pins the field (see external/brightdata/arxiv/README.md)
    and a pinned value must never be mistaken for the query that ran.
    """
    if not isinstance(raw, dict):
        raise ResearchRecordRejectedError(
            ResearchRejectionReason.INVALID_RECORD, "record is not an object"
        )

    arxiv_id = normalize_arxiv_id(raw.get("arxiv_id"))
    title = _require_text(raw.get("title"), field="title", max_len=_TITLE_MAX_LEN)
    abstract = _require_text(raw.get("abstract"), field="abstract")
    authors = normalize_authors(raw.get("authors"))
    categories = normalize_categories(raw.get("categories"))
    published_at = parse_publication_date(raw.get("published_at"))
    paper_url = normalize_arxiv_url(
        raw.get("paper_url"), field="paper_url", path_prefix=_ABS_PATH_PREFIX
    )
    pdf_url = normalize_arxiv_url(
        raw.get("pdf_url"), field="pdf_url", path_prefix=_PDF_PATH_PREFIX
    )
    _require_url_identifies(
        paper_url, arxiv_id=arxiv_id, field="paper_url", path_prefix=_ABS_PATH_PREFIX
    )
    _require_url_identifies(
        pdf_url, arxiv_id=arxiv_id, field="pdf_url", path_prefix=_PDF_PATH_PREFIX
    )

    return NormalizedResearchPaper(
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        authors=authors,
        categories=categories,
        published_at=published_at,
        paper_url=paper_url,
        pdf_url=pdf_url,
    )
