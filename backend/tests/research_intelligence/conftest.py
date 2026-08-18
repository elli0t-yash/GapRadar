"""Fixtures for the research-intelligence tests.

Every record here is either the committed Bright Data validation output
or a small synthetic variant of it, so these tests exercise the real
contract rather than an invented one. NOTHING in this package makes a
network call: acquisition is Bright Data's job and is not under test.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.models import Signal
from tests.api.conftest import (  # noqa: F401 - re-exported API fixtures
    api_client,
    brightdata_settings,
    make_api_client,
    scheduler,
)
from tests.opportunity_engine.conftest import (
    make_collector,
    make_run,
    make_signal,
    make_source,
)

# The validated collector output, owned by the Bright Data side and NOT
# tracked in git. Two consequences this has to handle:
#
# - the path moves. It has already moved once (sample-results.json ->
#   samples/dynamic-vehicle-routing.json) when the collector gained
#   dynamic queries, so candidates are tried in order rather than one
#   path being hardcoded;
# - it can be absent entirely, in a fresh clone or in CI, because it is
#   untracked. The fixture skips with a reason instead of erroring, so a
#   missing collaborator artifact never reads as a backend regression.
_ARXIV_ARTIFACTS = (
    Path(__file__).resolve().parents[3] / "external" / "brightdata" / "arxiv"
)
_ARXIV_SAMPLE_CANDIDATES = (
    _ARXIV_ARTIFACTS / "samples" / "dynamic-vehicle-routing.json",
    _ARXIV_ARTIFACTS / "sample-results.json",
)
ARXIV_SAMPLE_FIXTURE = next(
    (path for path in _ARXIV_SAMPLE_CANDIDATES if path.exists()),
    _ARXIV_SAMPLE_CANDIDATES[0],
)

VALIDATED_QUERY = "dynamic vehicle routing"


@pytest.fixture(scope="session")
def arxiv_records() -> list[dict[str, Any]]:
    """The validated collector output, verbatim.

    Skipped rather than failed when the artifact is absent: it belongs to
    the Bright Data side, is untracked, and its absence says nothing
    about whether GapRadar's ingestion is correct.
    """
    if not ARXIV_SAMPLE_FIXTURE.exists():
        pytest.skip(
            f"Bright Data arXiv sample not present at {ARXIV_SAMPLE_FIXTURE}; "
            "it is an untracked artifact owned by the collector side"
        )
    return json.loads(ARXIV_SAMPLE_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def records(arxiv_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A mutable per-test copy of the validated output."""
    return [dict(record) for record in arxiv_records]


def arxiv_record(**overrides: Any) -> dict[str, Any]:
    """One well-formed record, shaped exactly like the collector's output."""
    record: dict[str, Any] = {
        "arxiv_id": "2608.13083",
        "title": "AoI-Guaranteed Dynamic Route Planning for Connected Vehicles",
        "abstract": "A novel dual-factor approach to route planning.",
        "authors": ["Sajedeh Norouzi", "Maryam Ansarifard"],
        "published_at": "2026-08-13",
        "categories": ["Systems and Control (eess.SY)"],
        "paper_url": "https://arxiv.org/abs/2608.13083",
        "pdf_url": "https://arxiv.org/pdf/2608.13083",
        "query": VALIDATED_QUERY,
    }
    record.update(overrides)
    return record


def arxiv_record_for(arxiv_id: str, **overrides: Any) -> dict[str, Any]:
    """A record whose URLs correctly identify the given arXiv id."""
    return arxiv_record(
        arxiv_id=arxiv_id,
        paper_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        **overrides,
    )


def make_opportunity_signal(session: Session, *, title: str) -> Signal:
    """A trusted problem Signal -- what an Opportunity is computed from.

    Built through the opportunity-engine factories so these rows are
    identical to the ones the market side already persists.
    """
    source = make_source(session, name=f"Fix My Itch [{title}]")
    collector = make_collector(
        session, source, external_collector_id=f"c_{abs(hash(title)) % 10**8}"
    )
    run = make_run(session, collector, external_run_id=f"j_{abs(hash(title)) % 10**8}")
    return make_signal(session, source, run, title=title)


@pytest.fixture
def opportunity_signal(db_session: Session) -> Signal:
    return make_opportunity_signal(db_session, title="Cargo booking is broken")


@pytest.fixture
def second_opportunity_signal(db_session: Session) -> Signal:
    return make_opportunity_signal(db_session, title="Last-mile dispatch is opaque")
