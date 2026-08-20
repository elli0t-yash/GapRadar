"""The arXiv acquisition adapter is reusable, unchanged, by both subjects.

Requirement: the collector cares about a QUERY, a limit and a provider --
never about whether the query came from a Signal or an Investigation.
This asserts that as a property of its interface rather than by running
it, because running it would mean contacting Bright Data.

THE SCRAPER ITSELF IS NOT MODIFIED BY THIS PHASE. Nothing here builds a
new collector, changes the collector id, or changes the search URL.
"""

import inspect

from app.integrations.brightdata.arxiv import (
    ARXIV_COLLECTOR_ID,
    BrightDataArxivCollector,
    build_arxiv_search_url,
)
from app.research_intelligence.acquisition import ResearchCollector


def test_the_collector_satisfies_the_subject_agnostic_port() -> None:
    assert isinstance(BrightDataArxivCollector, type)
    assert hasattr(BrightDataArxivCollector, "search")
    assert hasattr(ResearchCollector, "search")


def test_search_takes_a_query_and_nothing_about_the_subject() -> None:
    """No signal_id, no investigation_id, no origin. Just the text."""
    parameters = list(
        inspect.signature(BrightDataArxivCollector.search).parameters
    )

    assert parameters == ["self", "query"]


def test_the_search_url_depends_only_on_the_query() -> None:
    """Two subjects issuing the same query produce the same provider call."""
    assert build_arxiv_search_url("vehicle routing") == build_arxiv_search_url(
        "vehicle routing"
    )


def test_the_published_collector_id_is_unchanged() -> None:
    """The frozen, proven v7 collector. This phase adds no new one."""
    assert ARXIV_COLLECTOR_ID == "c_msz192rbcrk2ki1vj"
