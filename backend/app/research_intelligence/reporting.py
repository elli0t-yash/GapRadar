"""Turning one engine result into the fields a run row persists.

Shared by both run lifecycles -- research enrichment for signals and
investigation runs for user hypotheses -- because the questions are
identical: how many papers made it through the funnel, what happened to
each query, why did this end the way it did, and what does a person read
when nothing came back.

Two copies of these would be the real fork. The counters especially: they
are the numbers a UI puts on screen, three of the four are unrecoverable
after the run ends, and a second implementation drifting by one field is
exactly how a run came to show "34 papers" while its own summary said 20.

Pure functions over a ResearchEnrichmentResult. No session, no ORM, no
provider.
"""

from app.domain.enums import ResearchOutcomeReason
from app.research_intelligence.orchestration import ResearchEnrichmentResult


def counters_from_result(result: ResearchEnrichmentResult) -> dict[str, int]:
    """The four counts that describe one run's funnel.

    THE ONE PLACE any of these numbers is computed. Each comes from the
    engine's own result rather than being reconstructed later, because
    three of the four are unrecoverable once the run ends: rejected
    verdicts are not persisted.

    `discovered` is the DISTINCT paper count, never the sum of per-query
    counts. A paper returned by two searches is one paper; summing them
    is what produced a UI reporting 34 papers for a run whose own summary
    said 20, and query-level counts stay in query_states for diagnostics
    rather than feeding this.

    `selected` and `judged` are kept apart on purpose. They differ
    whenever the semantic matcher declined on some candidates -- a
    provider failure mid-run -- and collapsing them would hide exactly
    that.
    """
    return {
        "discovered": result.candidate_paper_count,
        "selected": result.judged_paper_count,
        "judged": result.matches_created
        + result.matches_updated
        + result.matches_rejected,
        "matched": result.matches_created + result.matches_updated,
    }


def is_semantic_outage(
    result: ResearchEnrichmentResult, counters: dict[str, int]
) -> bool:
    """Did the judge break, as opposed to judging everything irrelevant?

    These look identical in the counters and mean opposite things. The
    ONLY thing that separates them is whether the matcher reported a
    FAILURE, so that is what is tested -- never `judged < selected`,
    which is also true of a normal run where some papers were declined on
    their merits or trimmed by the candidate cap.

    True means papers were selected and NOTHING came back. Calling that
    "no relevant research" would report a verdict the judge never gave.
    """
    return (
        result.judging_failures > 0
        and counters["selected"] > 0
        and counters["judged"] == 0
    )


def success_reason(
    result: ResearchEnrichmentResult,
) -> ResearchOutcomeReason | None:
    """Why a SUCCEEDED run is worth explaining, or None if it is not.

    An ordinary success with matches carries no reason: there is nothing
    to say beyond the research itself. The two that do carry one are the
    states the UI would otherwise have to guess at -- and would otherwise
    render as failures.
    """
    if result.is_partial:
        # Real research, built on less than the full plan. Stated, not
        # hidden, and still a success.
        return ResearchOutcomeReason.ACQUISITION_PARTIAL
    if result.matches_created + result.matches_updated == 0:
        # We looked properly and found nothing above the bar. An answer,
        # not a failure.
        return ResearchOutcomeReason.NO_RELEVANT_RESEARCH
    return None


def states_from_result(result: ResearchEnrichmentResult) -> list[dict[str, object]]:
    """Per-query state rebuilt from the finished engine result.

    The final write comes from the result rather than the live executions
    so the persisted state carries the post-ingestion paper counts, which
    only exist once ingestion has run.
    """
    return [
        {
            "query": outcome.query,
            "status": outcome.status.value,
            "provider_job_id": outcome.provider_job_id,
            "records_received": outcome.records_received,
            "papers_returned": outcome.papers_returned,
            "error": outcome.error,
            "elapsed_seconds": outcome.elapsed_seconds,
        }
        for outcome in result.queries
    ]


def all_searches_failed_message(result: ResearchEnrichmentResult) -> str:
    """Why a run with no usable search is being failed.

    Names the dominant reason so an operator can tell "the provider is
    down" from "these queries are bad" without opening the logs.
    """
    total = len(result.queries)
    timed_out = len(result.timed_out_queries)
    if timed_out == total:
        return (
            f"all {total} research searches timed out before returning; "
            "the provider jobs may still be running and this can be retried"
        )
    if timed_out:
        return (
            f"no research search returned: {timed_out} of {total} timed out and "
            f"{total - timed_out} failed"
        )
    return f"all {total} research searches failed before returning any papers"


__all__ = [
    "all_searches_failed_message",
    "counters_from_result",
    "is_semantic_outage",
    "states_from_result",
    "success_reason",
]
