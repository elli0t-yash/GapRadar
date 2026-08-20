"""Web discovery persistence: provenance, dedupe, isolation.

No network: every provider here is a local object. The properties under
test are the ones that decide whether the stored evidence can be trusted
-- one row per page, every query that found it retained, and nothing
about research touched.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Investigation,
    InvestigationCompetitor,
    InvestigationDemandEvidence,
    InvestigationWebSearchHit,
    InvestigationWebSearchRun,
    ResearchPaper,
    Signal,
)
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    WebSearchStatus,
)
from app.investigations.subject import research_subject_from_investigation
from app.investigations.web_intelligence import (
    discover_web_evidence,
    searches_that_found,
)
from app.research_intelligence.schemas import ResearchSubject
from app.web_intelligence.acquisition import (
    SequenceWebSearchProvider,
    WebSearchError,
)
from app.web_intelligence.classification import (
    CompetitorVerdict,
    DemandVerdict,
    LexicalCompetitorClassifier,
    LexicalDemandClassifier,
)
from app.web_intelligence.execution import PlannedWebSearch
from app.web_intelligence.schemas import (
    DEFAULT_LOCALE,
    WebIntelligenceRecord,
    WebSearchFamily,
)

DEMAND_A = "restaurant waste problems"
DEMAND_B = "restaurant waste challenges"
COMPETITOR_A = "restaurant waste software"


def record(url: str, query: str, *, title: str = "Page", position: int = 1):
    return WebIntelligenceRecord(
        query=query,
        title=title,
        url=url,
        domain=url.split("/")[2],
        snippet="restaurant inventory waste is a manual problem",
        position=position,
    )


class FixedDemandClassifier:
    def __init__(self, classification=DemandEvidenceClassification.SUPPORT, score=70.0):
        self.classification = classification
        self.score = score
        self.seen: list[str] = []

    def classify(self, *, subject: ResearchSubject, record: WebIntelligenceRecord):
        self.seen.append(record.url)
        return DemandVerdict(
            classification=self.classification,
            relevance_score=self.score,
            reason="fixed verdict for the test",
        )


class FixedCompetitorClassifier:
    def __init__(self, classification=CompetitorClassification.DIRECT, score=80.0):
        self.classification = classification
        self.score = score

    def classify(self, *, subject: ResearchSubject, record: WebIntelligenceRecord):
        return CompetitorVerdict(
            classification=self.classification,
            relevance_score=self.score,
            name=record.title,
            reason="fixed verdict for the test",
        )


class DecliningClassifier:
    """The judge is down: it declines and REPORTS that it failed."""

    def __init__(self) -> None:
        self.failures = 0

    def classify(self, *, subject: ResearchSubject, record: WebIntelligenceRecord):
        self.failures += 1


def discover(
    db_session: Session,
    investigation: Investigation,
    *,
    provider: Any,
    searches: list[PlannedWebSearch],
    demand_classifier: Any = None,
    competitor_classifier: Any = None,
):
    return discover_web_evidence(
        db_session,
        subject=research_subject_from_investigation(investigation),
        investigation_id=investigation.id,
        run_id=None,
        searches=searches,
        provider=provider,
        locale=DEFAULT_LOCALE,
        demand_classifier=demand_classifier or FixedDemandClassifier(),
        competitor_classifier=competitor_classifier or FixedCompetitorClassifier(),
        provider_name="fake",
        provider_product="fake_serp",
    )


def demand(*queries: str):
    return [PlannedWebSearch(query=q, family=WebSearchFamily.DEMAND) for q in queries]


def competitors(*queries: str):
    return [
        PlannedWebSearch(query=q, family=WebSearchFamily.COMPETITOR) for q in queries
    ]


def count(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# -- persistence ------------------------------------------------------------


def test_evidence_is_persisted_for_a_successful_search(
    db_session: Session, investigation: Investigation
) -> None:
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    results = discover(
        db_session, investigation, provider=provider, searches=demand(DEMAND_A)
    )

    assert results[WebSearchFamily.DEMAND].accepted == 1
    assert count(db_session, InvestigationDemandEvidence) == 1


def test_the_provider_execution_is_recorded(
    db_session: Session, investigation: Investigation
) -> None:
    """Observability, kept off the evidence row."""
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    discover(db_session, investigation, provider=provider, searches=demand(DEMAND_A))

    run = db_session.execute(select(InvestigationWebSearchRun)).scalar_one()
    assert run.query == DEMAND_A
    assert run.family == "demand"
    assert run.status is WebSearchStatus.SUCCEEDED
    assert run.records_returned == 1
    assert run.provider == "fake"
    assert run.product == "fake_serp"
    assert run.locale_country == "us"
    assert run.locale_language == "en"
    # Never fabricated: the synchronous SERP API supplies none.
    assert run.provider_request_id is None


def test_a_failed_search_is_recorded_too(
    db_session: Session, investigation: Investigation
) -> None:
    """Otherwise nobody can tell "never attempted" from "attempted and refused"."""
    provider = SequenceWebSearchProvider(
        {}, failures={DEMAND_A: WebSearchError(DEMAND_A, "provider refused")}
    )

    results = discover(
        db_session, investigation, provider=provider, searches=demand(DEMAND_A)
    )

    run = db_session.execute(select(InvestigationWebSearchRun)).scalar_one()
    assert run.status is WebSearchStatus.FAILED
    assert run.records_returned == 0
    assert run.error == "provider refused"
    assert results[WebSearchFamily.DEMAND].is_failed


def test_a_successful_empty_search_is_not_a_failure(
    db_session: Session, investigation: Investigation
) -> None:
    """Zero records with SUCCEEDED means the engine looked and found nothing."""
    provider = SequenceWebSearchProvider({DEMAND_A: []})

    results = discover(
        db_session, investigation, provider=provider, searches=demand(DEMAND_A)
    )

    run = db_session.execute(select(InvestigationWebSearchRun)).scalar_one()
    assert run.status is WebSearchStatus.SUCCEEDED
    assert run.records_returned == 0
    assert not results[WebSearchFamily.DEMAND].is_failed
    assert results[WebSearchFamily.DEMAND].queries_succeeded == 1


# -- provenance -------------------------------------------------------------


def test_a_page_found_by_two_queries_is_one_piece_of_evidence(
    db_session: Session, investigation: Investigation
) -> None:
    """Counting it twice would let one blog post look like a market."""
    url = "https://a.test/shared"
    provider = SequenceWebSearchProvider(
        {
            DEMAND_A: [record(url, DEMAND_A)],
            DEMAND_B: [record(url, DEMAND_B)],
        }
    )

    results = discover(
        db_session, investigation, provider=provider, searches=demand(DEMAND_A, DEMAND_B)
    )

    assert results[WebSearchFamily.DEMAND].candidates == 1
    assert count(db_session, InvestigationDemandEvidence) == 1


def test_every_query_that_found_a_page_is_retained(
    db_session: Session, investigation: Investigation
) -> None:
    """CROSS-QUERY PROVENANCE. Convergence is a real signal and is kept."""
    url = "https://a.test/shared"
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record(url, DEMAND_A)], DEMAND_B: [record(url, DEMAND_B)]}
    )

    discover(
        db_session, investigation, provider=provider, searches=demand(DEMAND_A, DEMAND_B)
    )

    assert count(db_session, InvestigationWebSearchHit) == 2
    found_by = searches_that_found(
        db_session, investigation_id=investigation.id, url=url
    )
    assert {run.query for run in found_by} == {DEMAND_A, DEMAND_B}


def test_a_page_found_by_a_demand_and_a_competitor_query_keeps_both(
    db_session: Session, investigation: Investigation
) -> None:
    """Two families converging is a fact worth keeping, not a conflict."""
    url = "https://a.test/shared"
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record(url, DEMAND_A)], COMPETITOR_A: [record(url, COMPETITOR_A)]}
    )

    discover(
        db_session,
        investigation,
        provider=provider,
        searches=[*demand(DEMAND_A), *competitors(COMPETITOR_A)],
    )

    found_by = searches_that_found(
        db_session, investigation_id=investigation.id, url=url
    )
    assert {run.family for run in found_by} == {"demand", "competitor"}
    # One judgement per family, in its own table.
    assert count(db_session, InvestigationDemandEvidence) == 1
    assert count(db_session, InvestigationCompetitor) == 1


def test_re_running_updates_the_verdict_without_duplicating_evidence(
    db_session: Session, investigation: Investigation
) -> None:
    url = "https://a.test/1"
    provider = SequenceWebSearchProvider({DEMAND_A: [record(url, DEMAND_A)]})

    discover(
        db_session,
        investigation,
        provider=provider,
        searches=demand(DEMAND_A),
        demand_classifier=FixedDemandClassifier(score=60.0),
    )
    discover(
        db_session,
        investigation,
        provider=provider,
        searches=demand(DEMAND_A),
        demand_classifier=FixedDemandClassifier(
            classification=DemandEvidenceClassification.STRONG_SUPPORT, score=95.0
        ),
    )

    assert count(db_session, InvestigationDemandEvidence) == 1
    row = db_session.execute(select(InvestigationDemandEvidence)).scalar_one()
    assert row.relevance_score == 95.0
    assert row.classification is DemandEvidenceClassification.STRONG_SUPPORT
    # But the history of how it was found only accumulates.
    assert count(db_session, InvestigationWebSearchHit) == 2


# -- semantics --------------------------------------------------------------


def test_an_irrelevant_page_is_judged_but_not_stored(
    db_session: Session, investigation: Investigation
) -> None:
    """It is counted, so the drop is visible rather than silent."""
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    results = discover(
        db_session,
        investigation,
        provider=provider,
        searches=demand(DEMAND_A),
        demand_classifier=FixedDemandClassifier(
            classification=DemandEvidenceClassification.IRRELEVANT
        ),
    )

    phase = results[WebSearchFamily.DEMAND]
    assert phase.judged == 1
    assert phase.accepted == 0
    assert phase.by_classification == {"irrelevant": 1}
    assert count(db_session, InvestigationDemandEvidence) == 0


def test_a_contradicting_page_is_kept(
    db_session: Session, investigation: Investigation
) -> None:
    """The findings most likely to change a founder's mind are not discarded."""
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    discover(
        db_session,
        investigation,
        provider=provider,
        searches=demand(DEMAND_A),
        demand_classifier=FixedDemandClassifier(
            classification=DemandEvidenceClassification.CONTRADICTS
        ),
    )

    row = db_session.execute(select(InvestigationDemandEvidence)).scalar_one()
    assert row.classification is DemandEvidenceClassification.CONTRADICTS


def test_a_declined_judgement_stores_nothing_and_is_reported(
    db_session: Session, investigation: Investigation
) -> None:
    """A judge that could not answer has said nothing about the page."""
    classifier = DecliningClassifier()
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    results = discover(
        db_session,
        investigation,
        provider=provider,
        searches=demand(DEMAND_A),
        demand_classifier=classifier,
    )

    phase = results[WebSearchFamily.DEMAND]
    assert phase.candidates == 1
    assert phase.judged == 0
    assert phase.classification_failures == 1
    assert count(db_session, InvestigationDemandEvidence) == 0


def test_a_competitor_name_defaults_to_the_page_title(
    db_session: Session, investigation: Investigation
) -> None:
    """Never an invented company name."""
    provider = SequenceWebSearchProvider(
        {COMPETITOR_A: [record("https://a.test/1", COMPETITOR_A, title="MarketMan")]}
    )

    discover(
        db_session,
        investigation,
        provider=provider,
        searches=competitors(COMPETITOR_A),
    )

    row = db_session.execute(select(InvestigationCompetitor)).scalar_one()
    assert row.name == "MarketMan"


def test_the_deterministic_classifiers_run_end_to_end(
    db_session: Session, investigation: Investigation
) -> None:
    """The shipped default path, with nothing mocked but the provider."""
    provider = SequenceWebSearchProvider(
        {
            DEMAND_A: [record("https://a.test/1", DEMAND_A)],
            COMPETITOR_A: [record("https://b.test/1", COMPETITOR_A)],
        }
    )

    results = discover(
        db_session,
        investigation,
        provider=provider,
        searches=[*demand(DEMAND_A), *competitors(COMPETITOR_A)],
        demand_classifier=LexicalDemandClassifier(),
        competitor_classifier=LexicalCompetitorClassifier(),
    )

    assert results[WebSearchFamily.DEMAND].judged == 1
    assert results[WebSearchFamily.COMPETITOR].judged == 1


# -- isolation from research ------------------------------------------------


def test_web_discovery_creates_no_research_paper(
    db_session: Session, investigation: Investigation
) -> None:
    """A web page is an observation, not a paper. The tables never mix."""
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    discover(db_session, investigation, provider=provider, searches=demand(DEMAND_A))

    assert count(db_session, ResearchPaper) == 0


def test_web_discovery_creates_no_signal(
    db_session: Session, investigation: Investigation
) -> None:
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]}
    )

    discover(db_session, investigation, provider=provider, searches=demand(DEMAND_A))

    assert count(db_session, Signal) == 0


def test_no_discovered_url_is_opened(
    db_session: Session, investigation: Investigation
) -> None:
    """DISCOVERY ONLY, asserted at the orchestration boundary.

    The provider is the only thing that could reach the network, and it
    is asked exactly once per planned query -- never once per result.
    """
    provider = SequenceWebSearchProvider(
        {
            DEMAND_A: [
                record("https://a.test/1", DEMAND_A),
                record("https://a.test/2", DEMAND_A),
                record("https://a.test/3", DEMAND_A),
            ]
        }
    )

    discover(db_session, investigation, provider=provider, searches=demand(DEMAND_A))

    assert provider.searched_queries == [DEMAND_A]


# -- partial failure --------------------------------------------------------


def test_a_failed_query_does_not_discard_what_the_others_found(
    db_session: Session, investigation: Investigation
) -> None:
    provider = SequenceWebSearchProvider(
        {DEMAND_A: [record("https://a.test/1", DEMAND_A)]},
        failures={DEMAND_B: WebSearchError(DEMAND_B, "provider refused")},
    )

    results = discover(
        db_session,
        investigation,
        provider=provider,
        searches=demand(DEMAND_A, DEMAND_B),
    )

    phase = results[WebSearchFamily.DEMAND]
    assert phase.is_partial
    assert phase.queries_succeeded == 1
    assert phase.queries_failed == 1
    assert phase.accepted == 1
    assert count(db_session, InvestigationDemandEvidence) == 1


def test_one_family_failing_does_not_affect_the_other(
    db_session: Session, investigation: Investigation
) -> None:
    provider = SequenceWebSearchProvider(
        {COMPETITOR_A: [record("https://b.test/1", COMPETITOR_A)]},
        failures={DEMAND_A: WebSearchError(DEMAND_A, "provider refused")},
    )

    results = discover(
        db_session,
        investigation,
        provider=provider,
        searches=[*demand(DEMAND_A), *competitors(COMPETITOR_A)],
    )

    assert results[WebSearchFamily.DEMAND].is_failed
    assert not results[WebSearchFamily.COMPETITOR].is_failed
    assert count(db_session, InvestigationCompetitor) == 1


def test_a_family_that_was_never_planned_is_absent_not_zero(
    db_session: Session, investigation: Investigation
) -> None:
    """"Not asked" and "asked and found nothing" are different facts."""
    provider = SequenceWebSearchProvider({DEMAND_A: []})

    results = discover(
        db_session, investigation, provider=provider, searches=demand(DEMAND_A)
    )

    assert WebSearchFamily.DEMAND in results
    assert WebSearchFamily.COMPETITOR not in results


@pytest.mark.parametrize(
    "family", [WebSearchFamily.DEMAND, WebSearchFamily.COMPETITOR]
)
def test_evidence_lands_in_the_table_its_family_owns(
    db_session: Session, investigation: Investigation, family: WebSearchFamily
) -> None:
    query = DEMAND_A if family is WebSearchFamily.DEMAND else COMPETITOR_A
    provider = SequenceWebSearchProvider({query: [record("https://a.test/1", query)]})

    discover(
        db_session,
        investigation,
        provider=provider,
        searches=[PlannedWebSearch(query=query, family=family)],
    )

    if family is WebSearchFamily.DEMAND:
        assert count(db_session, InvestigationDemandEvidence) == 1
        assert count(db_session, InvestigationCompetitor) == 0
    else:
        assert count(db_session, InvestigationCompetitor) == 1
        assert count(db_session, InvestigationDemandEvidence) == 0
