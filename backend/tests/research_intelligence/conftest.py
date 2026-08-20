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

from app.db.models import Investigation, Signal
from tests.api.conftest import (  # noqa: F401 - re-exported API fixtures
    api_client,
    brightdata_settings,
    enrichment_scheduler,
    investigation_scheduler,
    make_api_client,
    scheduler,
)
from tests.opportunity_engine.conftest import (
    make_collector,
    make_run,
    make_signal,
    make_source,
)

# Representative arXiv records, OWNED BY THE BACKEND and committed.
#
# Previously this pointed into external/brightdata/arxiv/, which is
# untracked and owned by the collector side. That made the backend test
# contract depend on a collaborator's working directory: the path moved
# once when the collector gained dynamic queries, and a fresh clone or CI
# had no file at all. Three records copied here -- chosen for shape
# variety, not volume -- pin the same nine-field contract deterministically.
ARXIV_SAMPLE_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "research" / "arxiv_records.json"
)

VALIDATED_QUERY = "dynamic vehicle routing"


@pytest.fixture(scope="session")
def arxiv_records() -> list[dict[str, Any]]:
    """Representative validated collector records, verbatim.

    Committed under the backend test tree, so this never skips and never
    depends on anything outside `backend/`.
    """
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


# The investigation fixture lives here too, so the coexistence tests can
# research a Signal and an Investigation side by side over the same
# committed records without either package importing the other's conftest
# in a cycle.
INVESTIGATION_QUERY = (
    "Booking cargo vehicles is harder than passenger transport and drivers "
    "quote inflated prices"
)


@pytest.fixture
def investigation(db_session: Session) -> Investigation:
    """One user-supplied hypothesis the deterministic generator can plan for."""
    from app.investigations.schemas import InvestigationCreate
    from app.investigations.service import create_investigation

    return create_investigation(
        db_session,
        payload=InvestigationCreate(query=INVESTIGATION_QUERY, industry="Logistics"),
    )
