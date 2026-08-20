"""Bounded concurrency and partial failure.

The bound is the point: six planned searches must not open six
simultaneous billable requests from one click.
"""

import threading
import time
from datetime import UTC, datetime

from app.web_intelligence.acquisition import (
    WebSearchError,
    WebSearchTimeoutError,
)
from app.web_intelligence.execution import (
    MAX_CONCURRENT_WEB_SEARCHES,
    PlannedWebSearch,
    run_web_searches,
)
from app.web_intelligence.schemas import (
    DEFAULT_LOCALE,
    SearchLocale,
    WebIntelligenceRecord,
    WebSearchFamily,
)


def record(url: str, query: str = "q") -> WebIntelligenceRecord:
    return WebIntelligenceRecord(
        query=query, title="T", url=url, domain="a.test", position=1
    )


class CountingProvider:
    """Records peak simultaneous in-flight searches."""

    def __init__(self, *, hold_seconds: float = 0.05) -> None:
        self.hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls: list[str] = []

    def search_web(self, query, *, limit=10, locale=None):
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.calls.append(query)
        try:
            time.sleep(self.hold_seconds)
            return [record(f"https://a.test/{query}", query)]
        finally:
            with self._lock:
                self.in_flight -= 1


class ScriptedProvider:
    """Succeeds, fails, or times out per query."""

    def __init__(self, *, results=None, failures=None, timeouts=()):
        self.results = results or {}
        self.failures = failures or {}
        self.timeouts = set(timeouts)
        self.calls: list[str] = []
        self.locales: list[SearchLocale | None] = []

    def search_web(self, query, *, limit=10, locale=None):
        self.calls.append(query)
        self.locales.append(locale)
        if query in self.timeouts:
            raise WebSearchTimeoutError(query, "provider did not answer in time")
        if query in self.failures:
            raise WebSearchError(query, self.failures[query])
        return self.results.get(query, [])


def planned(*queries: str, family=WebSearchFamily.DEMAND):
    return [PlannedWebSearch(query=query, family=family) for query in queries]


# -- the bound --------------------------------------------------------------


def test_no_more_than_four_searches_run_at_once() -> None:
    """An unbounded fan-out would open every request simultaneously."""
    provider = CountingProvider()
    searches = planned(*[f"query {index}" for index in range(12)])

    run_web_searches(provider=provider, locale=DEFAULT_LOCALE, searches=searches)

    assert provider.peak <= MAX_CONCURRENT_WEB_SEARCHES
    assert len(provider.calls) == 12


def test_the_bound_is_a_ceiling_a_caller_cannot_raise() -> None:
    """A caller asking for 50 still gets at most four."""
    provider = CountingProvider()

    run_web_searches(
        provider=provider,
        locale=DEFAULT_LOCALE,
        searches=planned(*[f"q{index}" for index in range(10)]),
        max_concurrency=50,
    )

    assert provider.peak <= MAX_CONCURRENT_WEB_SEARCHES


def test_searches_do_run_concurrently() -> None:
    """The bound is a cap, not a serialisation."""
    provider = CountingProvider()

    run_web_searches(
        provider=provider,
        locale=DEFAULT_LOCALE,
        searches=planned("a", "b", "c", "d"),
    )

    assert provider.peak > 1


def test_no_searches_is_no_work() -> None:
    provider = CountingProvider()
    assert run_web_searches(provider=provider, locale=DEFAULT_LOCALE, searches=[]) == []
    assert provider.calls == []


# -- ordering and outcomes --------------------------------------------------


def test_results_come_back_in_plan_order() -> None:
    """Whatever order they finished in -- dedupe must stay deterministic."""
    provider = CountingProvider()
    searches = planned("first", "second", "third")

    executions = run_web_searches(
        provider=provider, locale=DEFAULT_LOCALE, searches=searches
    )

    assert [execution.query for execution in executions] == [
        "first",
        "second",
        "third",
    ]


def test_a_successful_empty_search_is_not_a_failure() -> None:
    """THE DISTINCTION, at the execution layer."""
    provider = ScriptedProvider(results={"quiet": []})

    [execution] = run_web_searches(
        provider=provider, locale=DEFAULT_LOCALE, searches=planned("quiet")
    )

    assert execution.succeeded is True
    assert execution.records == []
    assert execution.error is None


def test_a_provider_failure_is_recorded_not_raised() -> None:
    provider = ScriptedProvider(failures={"broken": "provider refused"})

    [execution] = run_web_searches(
        provider=provider, locale=DEFAULT_LOCALE, searches=planned("broken")
    )

    assert execution.succeeded is False
    assert execution.records == []
    assert execution.error == "provider refused"


def test_a_timeout_is_distinguishable_from_a_failure() -> None:
    provider = ScriptedProvider(timeouts=["slow"], failures={"broken": "refused"})

    executions = run_web_searches(
        provider=provider, locale=DEFAULT_LOCALE, searches=planned("slow", "broken")
    )

    assert executions[0].timed_out is True
    assert executions[1].timed_out is False
    assert all(not execution.succeeded for execution in executions)


def test_partial_success_keeps_what_worked() -> None:
    """Discarding real evidence because one query failed is the worst outcome."""
    provider = ScriptedProvider(
        results={"good": [record("https://a.test/1")]},
        failures={"bad": "provider refused"},
    )

    executions = run_web_searches(
        provider=provider, locale=DEFAULT_LOCALE, searches=planned("good", "bad")
    )

    assert executions[0].succeeded and executions[0].records_returned == 1
    assert not executions[1].succeeded


def test_an_unexpected_provider_exception_is_absorbed() -> None:
    """A defect in an adapter must not take down the whole phase."""

    class ExplodingProvider:
        def search_web(self, query, *, limit=10, locale=None):
            raise RuntimeError("boom")

    [execution] = run_web_searches(
        provider=ExplodingProvider(),
        locale=DEFAULT_LOCALE,
        searches=planned("anything"),
    )

    assert execution.succeeded is False
    assert "RuntimeError" in (execution.error or "")


def test_latency_is_measured_per_search() -> None:
    provider = ScriptedProvider(results={"q": []})
    clock = iter(
        [datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
         datetime(2026, 8, 20, 12, 0, 2, tzinfo=UTC)]
    )

    [execution] = run_web_searches(
        provider=provider,
        locale=DEFAULT_LOCALE,
        searches=planned("q"),
        now=lambda: next(clock),
    )

    assert execution.latency_ms == 2000


def test_the_locale_is_passed_to_the_provider() -> None:
    provider = ScriptedProvider(results={"q": []})
    locale = SearchLocale(country="in", language="en")

    run_web_searches(
        provider=provider, locale=locale, searches=planned("q")
    )

    assert provider.locales == [locale]


def test_the_family_survives_execution() -> None:
    executions = run_web_searches(
        provider=ScriptedProvider(results={"a": [], "b": []}),
        locale=DEFAULT_LOCALE,
        searches=[
            PlannedWebSearch(query="a", family=WebSearchFamily.DEMAND),
            PlannedWebSearch(query="b", family=WebSearchFamily.COMPETITOR),
        ],
    )

    assert [execution.family for execution in executions] == [
        WebSearchFamily.DEMAND,
        WebSearchFamily.COMPETITOR,
    ]
