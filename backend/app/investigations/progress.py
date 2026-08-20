"""What one investigation run has actually done, phase by phase.

REPLACES A FLAT COUNTER BAG WITH A TYPED STRUCTURE, because the run now
has four phases whose numbers mean different things and must not be
added together. "18 judged" is meaningless without knowing whether it
counts papers or web pages.

Every number here is measured. There is deliberately no percentage, no
overall progress figure, and no whitespace or verdict phase: a phase that
reports 0/0 for work nobody has written is indistinguishable from a phase
that ran and found nothing, and that is the exact confusion this model
exists to prevent.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import PhaseState
from app.investigations.web_intelligence import WebPhaseResult
from app.research_intelligence.schemas import ResearchEnrichmentCounters
from app.web_intelligence.schemas import WebSearchFamily


class PlanningProgress(BaseModel):
    """Whether the run has a plan, and how big it is."""

    model_config = ConfigDict(frozen=True)

    state: PhaseState = PhaseState.PENDING
    research_queries: int = 0
    demand_queries: int = 0
    competitor_queries: int = 0


class ResearchPhaseProgress(BaseModel):
    """The academic phase: the existing funnel, with query progress.

    The four counters are exactly ResearchEnrichmentCounters -- the same
    type the opportunity path uses -- rather than a parallel set of four
    integers that could drift from it.
    """

    model_config = ConfigDict(frozen=True)

    state: PhaseState = PhaseState.PENDING
    queries_total: int = 0
    queries_completed: int = 0
    discovered: int = 0
    selected: int = 0
    judged: int = 0
    matched: int = 0

    @property
    def counters(self) -> ResearchEnrichmentCounters:
        """The funnel, in the shape the run row has always persisted."""
        return ResearchEnrichmentCounters(
            discovered=self.discovered,
            selected=self.selected,
            judged=self.judged,
            matched=self.matched,
        )


class WebPhaseProgress(BaseModel):
    """A web phase: how many searches ran and what survived judgement.

    `candidates` is DISTINCT urls, never the sum of per-query counts.
    `accepted` excludes pages judged irrelevant, which are counted in
    `judged` and in `by_classification` so the drop is visible rather
    than silent.
    """

    model_config = ConfigDict(frozen=True)

    state: PhaseState = PhaseState.PENDING
    queries_total: int = 0
    queries_completed: int = 0
    queries_succeeded: int = 0
    candidates: int = 0
    judged: int = 0
    accepted: int = 0
    # Counts keyed by the family's own classification vocabulary, e.g.
    # {"direct": 4, "adjacent": 5}. A dict rather than named columns
    # because the two families have different taxonomies and naming both
    # here would put competitor words on demand progress.
    by_classification: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_result(cls, result: WebPhaseResult) -> "WebPhaseProgress":
        """The persisted view of one family's discovery.

        State is derived from what happened, not asserted: every search
        failing is FAILED, some failing is PARTIAL, and anything else is
        COMPLETE. A caller cannot mark a phase complete that was not.
        """
        if result.is_failed:
            state = PhaseState.FAILED
        elif result.is_partial:
            state = PhaseState.PARTIAL
        else:
            state = PhaseState.COMPLETE
        return cls(
            state=state,
            queries_total=result.queries_total,
            queries_completed=result.queries_completed,
            queries_succeeded=result.queries_succeeded,
            candidates=result.candidates,
            judged=result.judged,
            accepted=result.accepted,
            by_classification=dict(result.by_classification),
        )


class InvestigationRunPhases(BaseModel):
    """Every phase of one run, as facts.

    Persisted whole on the run row and served by the polling endpoint.
    Phases that have not started read PENDING with zeroes, which is
    honest: the run exists and that phase has produced nothing yet.

    A phase whose provider is not configured reads SKIPPED, which is a
    third thing again -- no evidence and no failure.
    """

    model_config = ConfigDict(frozen=True)

    planning: PlanningProgress = Field(default_factory=PlanningProgress)
    research: ResearchPhaseProgress = Field(default_factory=ResearchPhaseProgress)
    demand: WebPhaseProgress = Field(default_factory=WebPhaseProgress)
    competitors: WebPhaseProgress = Field(default_factory=WebPhaseProgress)

    def with_web_results(
        self, results: dict[WebSearchFamily, WebPhaseResult]
    ) -> "InvestigationRunPhases":
        """This snapshot, updated with whatever the web phases produced.

        A family that was never planned keeps its existing state rather
        than being zeroed -- "not asked" is preserved.
        """
        updates: dict[str, WebPhaseProgress] = {}
        if WebSearchFamily.DEMAND in results:
            updates["demand"] = WebPhaseProgress.from_result(
                results[WebSearchFamily.DEMAND]
            )
        if WebSearchFamily.COMPETITOR in results:
            updates["competitors"] = WebPhaseProgress.from_result(
                results[WebSearchFamily.COMPETITOR]
            )
        return self.model_copy(update=updates)

    def to_payload(self) -> dict[str, object]:
        """The JSON-safe snapshot persisted on the run row."""
        return self.model_dump(mode="json")
