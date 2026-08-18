"""Contracts for the research side: what comes in, what is stored, what happened.

The input contract is the validated Bright Data arXiv collector output
(`external/brightdata/arxiv/schema.json`). Acquisition is not GapRadar's
concern -- something else fetches the records and hands the list over --
so nothing in this module knows about HTTP, Bright Data, or credentials.
"""

import enum
import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# One raw record exactly as the arXiv collector delivers it. Left as a
# plain dict rather than a strict model on purpose: it is UNTRUSTED
# provider output, and a pydantic model here would raise ValidationError
# for a bad field instead of producing the reason-coded RejectedRecord
# this layer promises. app.research_intelligence.normalizer is the single
# place that decides what a valid record is.
RawResearchRecord = dict[str, Any]


class ResearchRejectionReason(str, enum.Enum):
    """Why one record could not become a ResearchPaper.

    Mirrors app.ingestion.schemas.RejectionReason in shape and intent:
    stable codes so a caller can count and group failures without parsing
    English.
    """

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_ARXIV_ID = "invalid_arxiv_id"
    INVALID_URL = "invalid_url"
    INVALID_PUBLICATION_DATE = "invalid_publication_date"
    INVALID_AUTHORS = "invalid_authors"
    INVALID_CATEGORIES = "invalid_categories"
    INVALID_RECORD = "invalid_record"


class ResearchCategory(BaseModel):
    """One arXiv subject category, split into its two halves.

    arXiv publishes "Systems and Control (eess.SY)". The code is what a
    matcher filters on; the label is what a person reads. `code` is None
    when the source emitted something with no parenthesised code -- the
    text is kept as a label rather than discarded, because the source
    owns its own vocabulary and an unfamiliar shape is not a defect.
    """

    model_config = ConfigDict(frozen=True)

    code: str | None = None
    label: str


class NormalizedResearchPaper(BaseModel):
    """A validated, normalized paper, ready to construct a ResearchPaper row.

    Deliberately carries no `query`: a paper is an entity, and the query
    that happened to surface it is provenance about a search, not a
    property of the paper. See app.db.models.research_paper.
    """

    model_config = ConfigDict(frozen=True)

    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[ResearchCategory]
    published_at: date
    paper_url: str
    pdf_url: str

    @property
    def primary_category_code(self) -> str | None:
        """The first category's code, or None if it has none."""
        return self.categories[0].code if self.categories else None

    def category_payload(self) -> list[dict[str, Any]]:
        """Categories as the JSON shape the ResearchPaper column stores."""
        return [category.model_dump(mode="json") for category in self.categories]


class RejectedResearchRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    reason: ResearchRejectionReason
    detail: str
    # The record as received, preserved for debugging. Untrusted.
    raw: RawResearchRecord


class ResearchIngestionResult(BaseModel):
    """What one ingestion call did.

    `created` / `updated` / `unchanged` partition the records that
    normalized successfully and were not in-batch duplicates. The split
    matters: re-ingesting an identical batch must report `unchanged`, not
    `updated`, or idempotency is unobservable.
    """

    model_config = ConfigDict(frozen=True)

    search_run_id: uuid.UUID
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # The same arxiv_id appearing twice inside ONE batch. Not a count of
    # papers already in the database -- those are `unchanged`/`updated`.
    duplicates_in_batch: int = 0
    rejected: list[RejectedResearchRecord] = Field(default_factory=list)
    # Every paper this search resolved to, in the order returned.
    research_paper_ids: list[uuid.UUID] = Field(default_factory=list)

    @property
    def accepted(self) -> int:
        """Records that became a paper this call, new or existing."""
        return self.created + self.updated + self.unchanged
