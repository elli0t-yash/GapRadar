"""Research Intelligence: store research papers and how they were found.

Deliberately THIN. It re-exports the pure contract layer only -- schemas
and the normalizer -- and never imports app.research_intelligence.service
at package-import time.

That is not stylistic. `service` reaches app.db.models, which registers
every ORM model; pulling that in from a package __init__ is exactly the
shape that already broke a fresh-process import of app.pipeline.executor
once (see app/opportunity_engine/__init__.py, since trimmed for the same
reason). Import the service module by path:

    from app.research_intelligence.service import ingest_arxiv_search_results
"""

from app.research_intelligence.normalizer import (
    ResearchRecordRejectedError,
    normalize_arxiv_id,
    normalize_arxiv_record,
    normalize_arxiv_url,
    normalize_authors,
    normalize_categories,
    parse_publication_date,
)
from app.research_intelligence.schemas import (
    NormalizedResearchPaper,
    RawResearchRecord,
    RejectedResearchRecord,
    ResearchCategory,
    ResearchIngestionResult,
    ResearchRejectionReason,
)

__all__ = [
    "NormalizedResearchPaper",
    "RawResearchRecord",
    "RejectedResearchRecord",
    "ResearchCategory",
    "ResearchIngestionResult",
    "ResearchRecordRejectedError",
    "ResearchRejectionReason",
    "normalize_arxiv_id",
    "normalize_arxiv_record",
    "normalize_arxiv_url",
    "normalize_authors",
    "normalize_categories",
    "parse_publication_date",
]
