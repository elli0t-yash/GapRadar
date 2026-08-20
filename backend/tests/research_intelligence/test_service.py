"""Papers are upserted; searches accumulate. The distinction is the point.

No test here reaches Bright Data: records arrive as plain lists, which is
exactly how a future BrightDataArxivClient will hand them over.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Investigation,
    ResearchPaper,
    ResearchSearchResult,
    ResearchSearchRun,
    Signal,
)
from app.domain.enums import ResearchSource
from app.research_intelligence.schemas import (
    ResearchIngestionResult,
    ResearchRejectionReason,
)
from app.research_intelligence.service import (
    get_paper_by_arxiv_id,
    ingest_arxiv_search_results,
)
from tests.research_intelligence.conftest import (
    VALIDATED_QUERY,
    arxiv_record,
    arxiv_record_for,
)

SEARCHED_AT = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


def count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def as_utc(value: datetime) -> datetime:
    """Attach UTC to a timestamp GapRadar itself wrote as UTC.

    SQLite -- used by this suite -- hands a DateTime(timezone=True) column
    back without its offset, so a round-tripped value compares unequal to
    the aware one that went in. The same normalization
    app.schemas.reliability._utc and app.recallguard.service._as_utc
    already apply to GapRadar's own timestamps; provider-supplied ones are
    never treated this leniently.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# Every research search names EXACTLY ONE subject -- ResearchSearchRun's
# CHECK enforces it, and `ingest_arxiv_search_results` refuses a call that
# names none. Most tests here are about paper normalization, dedupe and
# transaction boundaries rather than about provenance, so they bind one
# subject once through this fixture instead of repeating it. The tests
# that ARE about provenance call the real function directly.
Ingest = Callable[..., ResearchIngestionResult]


@pytest.fixture
def ingest(db_session: Session, opportunity_signal: Signal) -> Ingest:
    def _ingest(session: Session, **kwargs: Any) -> ResearchIngestionResult:
        return ingest_arxiv_search_results(
            session, signal_id=opportunity_signal.id, **kwargs
        )

    return _ingest


# -- valid ingestion --------------------------------------------------------


def test_a_validated_batch_is_ingested_whole(
    db_session: Session, records: list[dict[str, Any]],
    ingest: Ingest,
) -> None:
    """The 15 real records, end to end, with no provider involved."""
    result = ingest(
        db_session, query=VALIDATED_QUERY, records=records, searched_at=SEARCHED_AT
    )

    expected = len(records)
    assert result.created == expected
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.rejected == []
    assert result.accepted == expected
    assert len(result.research_paper_ids) == expected
    assert count(db_session, ResearchPaper) == expected
    assert count(db_session, ResearchSearchResult) == expected


def test_a_persisted_paper_keeps_every_normalized_field(db_session: Session, ingest: Ingest) -> None:
    ingest(
        db_session, query=VALIDATED_QUERY, records=[arxiv_record()]
    )

    paper = get_paper_by_arxiv_id(db_session, arxiv_id="2608.13083")
    assert paper is not None
    assert paper.source is ResearchSource.ARXIV
    assert paper.title.startswith("AoI-Guaranteed")
    assert paper.authors == ["Sajedeh Norouzi", "Maryam Ansarifard"]
    assert paper.categories == [{"code": "eess.SY", "label": "Systems and Control"}]
    assert paper.primary_category_code == "eess.SY"
    assert paper.published_at == date(2026, 8, 13)
    assert paper.paper_url == "https://arxiv.org/abs/2608.13083"
    assert paper.pdf_url == "https://arxiv.org/pdf/2608.13083"


def test_result_order_is_preserved_as_the_search_ranking(
    db_session: Session, records: list[dict[str, Any]],
    ingest: Ingest,
) -> None:
    result = ingest(
        db_session, query=VALIDATED_QUERY, records=records
    )

    rows = list(
        db_session.execute(
            select(ResearchSearchResult)
            .where(ResearchSearchResult.research_search_run_id == result.search_run_id)
            .order_by(ResearchSearchResult.position)
        ).scalars()
    )
    assert [row.position for row in rows] == list(range(len(records)))
    assert [row.research_paper_id for row in rows] == result.research_paper_ids


# -- idempotency and upsert -------------------------------------------------


def test_re_ingesting_the_same_batch_creates_no_new_papers(
    db_session: Session, records: list[dict[str, Any]],
    ingest: Ingest,
) -> None:
    """Finding a paper again is not a new paper."""
    first = ingest(
        db_session, query=VALIDATED_QUERY, records=records
    )
    second = ingest(
        db_session, query=VALIDATED_QUERY, records=records
    )

    expected = len(records)
    assert first.created == expected
    assert second.created == 0
    assert second.updated == 0
    # Observably a no-op on the papers -- not "updated", which would churn
    # updated_at on every search and make idempotency untestable.
    assert second.unchanged == expected
    assert count(db_session, ResearchPaper) == expected
    # The search genuinely happened twice, so both runs are recorded.
    assert count(db_session, ResearchSearchRun) == 2
    assert count(db_session, ResearchSearchResult) == expected * 2


def test_a_revised_paper_updates_the_existing_row(db_session: Session, ingest: Ingest) -> None:
    ingest(
        db_session, query=VALIDATED_QUERY, records=[arxiv_record()]
    )
    result = ingest(
        db_session,
        query=VALIDATED_QUERY,
        records=[arxiv_record(title="AoI-Guaranteed Dynamic Route Planning, Revised")],
    )

    assert result.created == 0
    assert result.updated == 1
    assert count(db_session, ResearchPaper) == 1
    paper = get_paper_by_arxiv_id(db_session, arxiv_id="2608.13083")
    assert paper is not None
    assert paper.title.endswith("Revised")


def test_a_new_version_of_a_paper_is_the_same_paper(db_session: Session, ingest: Ingest) -> None:
    """v1 and v2 are revisions, not two papers."""
    ingest(
        db_session, query=VALIDATED_QUERY, records=[arxiv_record()]
    )
    ingest(
        db_session,
        query=VALIDATED_QUERY,
        records=[
            arxiv_record(
                arxiv_id="2608.13083v2",
                paper_url="https://arxiv.org/abs/2608.13083v2",
                pdf_url="https://arxiv.org/pdf/2608.13083v2",
            )
        ],
    )

    assert count(db_session, ResearchPaper) == 1


def test_the_same_paper_twice_in_one_batch_is_counted_not_duplicated(
    db_session: Session,
    ingest: Ingest,
) -> None:
    result = ingest(
        db_session,
        query=VALIDATED_QUERY,
        records=[arxiv_record(), arxiv_record(), arxiv_record_for("2607.22582")],
    )

    assert result.created == 2
    assert result.duplicates_in_batch == 1
    assert count(db_session, ResearchPaper) == 2
    # One result row per paper: position would be a lie for the second copy.
    assert count(db_session, ResearchSearchResult) == 2


# -- search provenance ------------------------------------------------------


def test_a_search_records_who_asked_what_and_when(
    db_session: Session, opportunity_signal: Signal
) -> None:
    result = ingest_arxiv_search_results(
        db_session,
        query="cargo vehicle booking",
        records=[arxiv_record()],
        signal_id=opportunity_signal.id,
        searched_at=SEARCHED_AT,
        provider_job_id="j_msyy0tyn18aapzwpey",
    )

    run = db_session.get(ResearchSearchRun, result.search_run_id)
    assert run is not None
    assert run.signal_id == opportunity_signal.id
    assert run.query == "cargo vehicle booking"
    assert as_utc(run.searched_at) == SEARCHED_AT
    assert run.provider_job_id == "j_msyy0tyn18aapzwpey"
    assert run.source is ResearchSource.ARXIV
    assert [r.research_paper_id for r in run.results] == result.research_paper_ids


def test_a_search_naming_no_subject_is_refused(
    db_session: Session, records: list[dict[str, Any]]
) -> None:
    """An unattributed search is not recorded as an orphan. It is refused.

    This reverses an earlier decision, deliberately. A row naming no
    subject cannot be read back through any read model and cannot be
    explained by any operator -- it is a provider call that happened and
    that nothing accounts for. Recording it "honestly" made the
    provenance table look complete while quietly accumulating rows no
    surface could ever show.
    """
    with pytest.raises(ValueError, match="exactly one subject"):
        ingest_arxiv_search_results(
            db_session, query="exploratory", records=records
        )


def test_a_search_naming_two_subjects_is_refused(
    db_session: Session,
    records: list[dict[str, Any]],
    opportunity_signal: Signal,
    investigation: Investigation,
) -> None:
    """A row naming both makes "which problem was this for" unanswerable."""
    with pytest.raises(ValueError, match="exactly one subject"):
        ingest_arxiv_search_results(
            db_session,
            query="ambiguous",
            records=records,
            signal_id=opportunity_signal.id,
            investigation_id=investigation.id,
        )


def test_a_refused_search_writes_nothing(
    db_session: Session, records: list[dict[str, Any]]
) -> None:
    """The guard runs before the INSERT, so nothing half-lands."""
    with pytest.raises(ValueError):
        ingest_arxiv_search_results(db_session, query="orphan", records=records)

    assert count(db_session, ResearchSearchRun) == 0
    assert count(db_session, ResearchPaper) == 0


def test_a_search_can_be_attributed_to_an_investigation(
    db_session: Session,
    records: list[dict[str, Any]],
    investigation: Investigation,
) -> None:
    """The second kind of subject, through the same entry point."""
    result = ingest_arxiv_search_results(
        db_session,
        query="cargo vehicle booking",
        records=records,
        investigation_id=investigation.id,
    )

    run = db_session.get(ResearchSearchRun, result.search_run_id)
    assert run is not None
    assert run.investigation_id == investigation.id
    assert run.signal_id is None


def test_one_paper_found_by_two_queries_is_one_paper_and_two_searches(
    db_session: Session, opportunity_signal: Signal, second_opportunity_signal: Signal
) -> None:
    """The reason `query` is not a column on ResearchPaper."""
    first = ingest_arxiv_search_results(
        db_session,
        query="dynamic vehicle routing",
        records=[arxiv_record()],
        signal_id=opportunity_signal.id,
    )
    second = ingest_arxiv_search_results(
        db_session,
        query="urban fleet dispatch",
        records=[arxiv_record()],
        signal_id=second_opportunity_signal.id,
    )

    assert count(db_session, ResearchPaper) == 1
    assert first.research_paper_ids == second.research_paper_ids

    paper = get_paper_by_arxiv_id(db_session, arxiv_id="2608.13083")
    assert paper is not None
    queries = {
        db_session.get(ResearchSearchRun, r.research_search_run_id).query
        for r in paper.search_results
    }
    assert queries == {"dynamic vehicle routing", "urban fleet dispatch"}
    assert len(paper.search_results) == 2


def test_the_query_comes_from_the_caller_not_from_the_record(
    db_session: Session,
    ingest: Ingest,
) -> None:
    """The collector pins its own `query`; trusting it would mislabel searches."""
    result = ingest(
        db_session,
        query="what was actually asked",
        records=[arxiv_record(query="what the collector had hardcoded")],
    )

    run = db_session.get(ResearchSearchRun, result.search_run_id)
    assert run is not None
    assert run.query == "what was actually asked"


# -- rejection --------------------------------------------------------------


def test_a_bad_record_is_reported_and_the_rest_still_land(
    db_session: Session,
    ingest: Ingest,
) -> None:
    result = ingest(
        db_session,
        query=VALIDATED_QUERY,
        records=[
            arxiv_record(),
            arxiv_record(arxiv_id="not-an-id"),
            arxiv_record_for("2607.22582", published_at="not a date"),
            arxiv_record_for("2607.10173"),
        ],
    )

    assert result.created == 2
    assert [r.index for r in result.rejected] == [1, 2]
    assert result.rejected[0].reason is ResearchRejectionReason.INVALID_ARXIV_ID
    assert result.rejected[1].reason is ResearchRejectionReason.INVALID_PUBLICATION_DATE
    # The raw record is preserved verbatim for debugging.
    assert result.rejected[0].raw["arxiv_id"] == "not-an-id"
    assert count(db_session, ResearchPaper) == 2
    assert count(db_session, ResearchSearchResult) == 2


def test_an_all_bad_batch_still_records_that_the_search_happened(
    db_session: Session,
    ingest: Ingest,
) -> None:
    """A search that returned nothing usable is evidence, not a non-event."""
    result = ingest(
        db_session, query=VALIDATED_QUERY, records=[arxiv_record(arxiv_id="junk")]
    )

    assert result.created == 0
    assert len(result.rejected) == 1
    assert db_session.get(ResearchSearchRun, result.search_run_id) is not None
    assert count(db_session, ResearchPaper) == 0


def test_an_empty_batch_is_a_recorded_search_with_no_results(
    db_session: Session,
    ingest: Ingest,
) -> None:
    result = ingest(
        db_session, query="a query that found nothing", records=[]
    )

    assert result.accepted == 0
    assert db_session.get(ResearchSearchRun, result.search_run_id) is not None
    assert count(db_session, ResearchSearchResult) == 0


# -- transaction boundary ---------------------------------------------------


def test_commit_false_leaves_the_transaction_to_the_caller(
    db_session: Session,
    ingest: Ingest,
) -> None:
    """Matches app.ingestion.service: the caller can extend the unit of work."""
    ingest(
        db_session, query=VALIDATED_QUERY, records=[arxiv_record()], commit=False
    )
    assert count(db_session, ResearchPaper) == 1

    db_session.rollback()

    assert count(db_session, ResearchPaper) == 0
    assert count(db_session, ResearchSearchRun) == 0
