"""The LOCAL executor for investigation runs. Not a job queue.

What this is: a way to run a claimed investigation in this process after
the HTTP response has been sent, so `POST /investigations/{id}/run`
returns in milliseconds while the Bright Data searches and the semantic
judging take as long as they take.

What this is NOT: durable. This runs on FastAPI's BackgroundTasks, which
is an in-process task and nothing more. IF RAILWAY RESTARTS MID-RUN the
task is gone, the Bright Data jobs keep running remotely and their
results are never collected, and the row is left QUEUED or RUNNING. There
is no retry and no broker, and pretending otherwise would be worse than
the honest limitation. `reconcile_stale_investigation_runs` is what makes
such a row terminal again so the user can retry -- without it, the
partial unique index would leave that investigation permanently
un-runnable.

What makes that survivable is that the run's state is PERSISTED and the
work is idempotent: papers upsert by arxiv_id and matches upsert by
(investigation_id, research_paper_id), so a stranded run can be
superseded by a fresh one without duplicating anything. The user-visible
cost of a crash is a stuck status, not corrupted intelligence.

Replacing this with a real worker later means replacing this file and
nothing else: app.investigations.execution knows nothing about how it is
called.
"""

import logging
import uuid

logger = logging.getLogger(__name__)


def execute_investigation_run(run_id: uuid.UUID) -> None:
    """Run one claimed investigation to completion, out of band.

    Opens its own Session and its own provider clients rather than
    borrowing the request's: by the time this runs the response has been
    sent and the request-scoped dependencies are already closed.

    Never raises. This is the top of a background task, so an escaping
    exception would be swallowed by the event loop with no traceback. The
    run's own terminal state is written by execute_run, so a failure here
    does not lose the record of what happened -- and if even that fails,
    the row is left active and the log is the only trace, which is why
    this logs rather than passes.

    The provider clients are constructed HERE, at the outermost edge, so
    that nothing in app.investigations or app.research_intelligence has
    to import one.
    """
    # Imported inside the function so importing this module never drags a
    # provider SDK into a process that will not run anything.
    from app.config import get_settings
    from app.db.session import get_session_factory
    from app.integrations.brightdata.arxiv import BrightDataArxivCollector
    from app.integrations.brightdata.client import BrightDataClient
    from app.integrations.brightdata.serp import (
        PROVIDER_NAME,
        PROVIDER_PRODUCT,
        BrightDataSerpWebSearchProvider,
    )
    from app.integrations.openai.errors import SemanticJudgeUnavailableError
    from app.integrations.openai.query_generator import OpenAIResearchQueryGenerator
    from app.integrations.openai.semantic_matcher import OpenAISemanticMatcher
    from app.integrations.openai.web_evidence import (
        OpenAICompetitorClassifier,
        OpenAIDemandClassifier,
    )
    from app.investigations.execution import execute_run
    from app.research_intelligence.background import research_polling_policy

    session = get_session_factory()()
    try:
        matcher = None
        try:
            matcher = OpenAISemanticMatcher()
        except SemanticJudgeUnavailableError:
            # No key configured. The engine falls back to its
            # deterministic development matcher rather than failing the
            # run, and the log says which one ran.
            logger.warning(
                "investigation_semantic_matcher_unavailable",
                extra={"run_id": str(run_id)},
            )

        fallback_generator = None
        try:
            fallback_generator = OpenAIResearchQueryGenerator()
        except SemanticJudgeUnavailableError:
            # Same trade. Without the fallback, an investigation whose
            # wording is outside the deterministic lexicon fails with
            # QUERY_PLAN_UNAVAILABLE instead of getting a second chance
            # -- honest, and strictly better than a crash.
            logger.warning(
                "investigation_query_fallback_unavailable",
                extra={"run_id": str(run_id)},
            )

        settings = get_settings()

        demand_classifier = None
        competitor_classifier = None
        try:
            demand_classifier = OpenAIDemandClassifier()
            competitor_classifier = OpenAICompetitorClassifier()
        except SemanticJudgeUnavailableError:
            # No key. The engine falls back to its deterministic lexical
            # classifiers, which are honest about being non-semantic,
            # rather than failing the run.
            logger.warning(
                "investigation_web_classifier_unavailable",
                extra={"run_id": str(run_id)},
            )

        # WEB DISCOVERY IS OPTIONAL. Without a SERP zone the run still
        # does its research and reports the web phases SKIPPED -- no
        # evidence and no failure, which is the honest third state.
        web_provider = (
            BrightDataSerpWebSearchProvider()
            if settings.BRIGHTDATA_SERP_ZONE
            else None
        )
        if web_provider is None:
            logger.warning(
                "investigation_web_provider_unconfigured",
                extra={"run_id": str(run_id)},
            )

        try:
            with BrightDataClient() as client:
                execute_run(
                    session,
                    run_id=run_id,
                # The research-specific budget, NOT the generic 900s
                # collection default -- the same policy the opportunity
                # path uses, from the same place, so a change to one is a
                # change to both.
                    collector=BrightDataArxivCollector(
                        client, polling=research_polling_policy()
                    ),
                    matcher=matcher,
                    fallback_generator=fallback_generator,
                    web_provider=web_provider,
                    demand_classifier=demand_classifier,
                    competitor_classifier=competitor_classifier,
                    provider_name=PROVIDER_NAME,
                    provider_product=PROVIDER_PRODUCT,
                    acquisition_budget_seconds=(
                        settings.RESEARCH_ACQUISITION_BUDGET_SECONDS
                    ),
                )
        finally:
            if web_provider is not None:
                web_provider.close()
    except Exception:
        logger.exception(
            "investigation_run_background_execution_failed",
            extra={"run_id": str(run_id)},
        )
    finally:
        session.close()
