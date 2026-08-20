"""Independent investigations: user-supplied ideas GapRadar can look into.

WHAT THIS SURFACE IS NOT. It is not the opportunity feed. Everything
under /opportunities is externally discovered evidence that survived
collection, source-contract validation and RecallGuard; everything here
is a sentence a user typed. The two are kept on separate paths, backed by
separate tables, so nothing can accidentally present one as the other.

NO GET HERE CONTACTS A PROVIDER, AND NEITHER DOES CREATE. Recording an
investigation writes one row and returns; reading its research reads
persisted rows and nothing else. Exactly ONE endpoint on this surface
spends money -- POST /{id}/run -- and it is only ever reached by an
explicit user action, never as a side effect of someone looking.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, status

from app.api.v1.deps import DbSession, InvestigationScheduler
from app.db.models import Investigation
from app.domain.enums import ResearchSubjectOrigin
from app.exceptions import AppError
from app.investigations.evidence import (
    DEFAULT_LIMIT as EVIDENCE_DEFAULT_LIMIT,
)
from app.investigations.evidence import (
    MAX_LIMIT as EVIDENCE_MAX_LIMIT,
)
from app.investigations.evidence import (
    get_competitors,
    get_demand_evidence,
)
from app.investigations.runs import (
    latest_run,
    reconcile_stale_investigation_runs,
    start_run,
)
from app.investigations.schemas import (
    CompetitorCollection,
    DemandEvidenceCollection,
    InvestigationCreate,
    InvestigationRead,
    InvestigationRunAccepted,
    InvestigationRunRead,
)
from app.investigations.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    create_investigation,
    get_investigation,
    list_investigations,
)
from app.research_intelligence.schemas import ResearchIntelligence
from app.research_intelligence.service import get_subject_research_intelligence

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.post(
    "", response_model=InvestigationRead, status_code=status.HTTP_201_CREATED
)
def create_investigation_request(
    payload: InvestigationCreate, session: DbSession
) -> Investigation:
    """Record an investigation. Do not investigate it.

    201, not 202: something was created and nothing was started. A 202
    would promise work is under way, and no work is -- the investigation
    engine does not exist yet, and when it does it will be reached by an
    explicit request, so that a user can never buy a provider run by
    typing into a box.

    The returned row is DRAFT for the same reason.
    """
    return create_investigation(session, payload=payload)


@router.get("", response_model=list[InvestigationRead])
def list_investigation_requests(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[Investigation]:
    """Recent investigations, newest first."""
    return list_investigations(session, limit=limit)


@router.get("/{investigation_id}", response_model=InvestigationRead)
def get_investigation_request(
    investigation_id: uuid.UUID, session: DbSession
) -> Investigation:
    """One investigation, exactly as it was submitted."""
    investigation = get_investigation(session, investigation_id=investigation_id)
    if investigation is None:
        raise AppError(
            f"investigation {investigation_id} not found", status_code=404
        )
    return investigation


@router.post(
    "/{investigation_id}/run",
    response_model=InvestigationRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_investigation_run(
    investigation_id: uuid.UUID,
    session: DbSession,
    background: BackgroundTasks,
    schedule: InvestigationScheduler,
) -> InvestigationRunAccepted:
    """Ask GapRadar to investigate this idea.

    THE ONLY PATH ON THIS SURFACE THAT SPENDS A PROVIDER CALL, and it is
    only ever reached by an explicit user action. Nothing about creating
    or opening an investigation triggers it.

    202, not 200: nothing has been searched or judged when this returns.
    The work inside the request is one lookup and one INSERT -- no Bright
    Data call, no LLM call, no waiting.

    Two answers, both 202, because in both the caller's request ("look
    into this") is satisfied:

    - `already_running` -- a run is in flight. It is returned as-is and
      nothing new is scheduled, so a double-click, two tabs, or a
      re-rendered effect cannot buy a second set of searches.
    - otherwise -- a run was claimed and handed to the executor.

    Unlike the opportunity path there is no `already_enriched` short
    circuit. An investigation is the user's own hypothesis and re-running
    it is a legitimate thing to want; the work is idempotent (papers
    upsert by arxiv_id, verdicts upsert by (investigation, paper)), so a
    second run refreshes rather than duplicates.

    Stale runs are reconciled first, so an investigation stranded by a
    backend restart can be run again rather than being blocked forever by
    the active-run index.
    """
    investigation = get_investigation(session, investigation_id=investigation_id)
    if investigation is None:
        raise AppError(
            f"investigation {investigation_id} not found", status_code=404
        )

    reconcile_stale_investigation_runs(session)

    run, already_running = start_run(session, investigation=investigation)

    if not already_running:
        # Runs after the response is sent, in this process. Local only,
        # and deliberately not treated as durable -- see
        # app.investigations.background.
        background.add_task(schedule, run.id)

    return InvestigationRunAccepted(
        run_id=run.id,
        investigation_id=investigation_id,
        status=run.status,
        already_running=already_running,
    )


@router.get("/{investigation_id}/run", response_model=InvestigationRunRead | None)
def get_investigation_run(
    investigation_id: uuid.UUID, session: DbSession
) -> InvestigationRunRead | None:
    """Where this investigation's most recent run has got to.

    Read-only, cheap, and safe to poll. Returns null when no run has ever
    been requested -- a different fact from a run that exists and is
    queued, which lets the client tell "never asked" from "asked and
    waiting" after a page reload.

    Reconciles abandoned runs first. FastAPI BackgroundTasks die with the
    process, so a restart mid-run strands a row as RUNNING -- and because
    the active-run index is what stops duplicate provider spend, a
    stranded row would block this investigation from ever running again.
    Ageing it out here means the client that is already polling is the
    thing that unsticks it, with no scheduler to own.
    """
    if get_investigation(session, investigation_id=investigation_id) is None:
        raise AppError(
            f"investigation {investigation_id} not found", status_code=404
        )

    reconcile_stale_investigation_runs(session)
    run = latest_run(session, investigation_id=investigation_id)
    return None if run is None else InvestigationRunRead.model_validate(run)


@router.get("/{investigation_id}/research", response_model=ResearchIntelligence)
def get_investigation_research(
    investigation_id: uuid.UUID, session: DbSession
) -> ResearchIntelligence:
    """The research GapRadar has connected to this investigation.

    READ ONLY, AND STRICTLY SO. This reads persisted rows and nothing
    else: no Bright Data call, no search, no matching. A GET that quietly
    triggered a provider run would make page loads cost money and would
    make an idempotent-looking request mutate the database.

    The same shape the opportunity surface returns, deliberately, so a
    frontend or an MCP client reads research without knowing which kind
    of subject produced it. `origin` is there for the consumers that
    legitimately care -- an investigation's research is about a
    hypothesis nobody has corroborated, and saying so is honest.

    An investigation that has never been run returns an empty-but-valid
    result, which is a different fact from a run that found nothing and
    reads as one.
    """
    if get_investigation(session, investigation_id=investigation_id) is None:
        raise AppError(
            f"investigation {investigation_id} not found", status_code=404
        )

    return get_subject_research_intelligence(
        session,
        subject_id=investigation_id,
        origin=ResearchSubjectOrigin.INVESTIGATION,
    )


@router.get("/{investigation_id}/evidence", response_model=DemandEvidenceCollection)
def get_investigation_evidence(
    investigation_id: uuid.UUID,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=EVIDENCE_MAX_LIMIT)] = EVIDENCE_DEFAULT_LIMIT,
) -> DemandEvidenceCollection:
    """Web pages judged as evidence that this problem is really experienced.

    READ ONLY, AND STRICTLY SO. Persisted rows and nothing else: no SERP
    request, no classification, and above all NO FETCHING OF THE PAGES
    LISTED. Discovery stores titles and snippets; opening those URLs is a
    separate later stage, and doing it on a read would turn a page load
    into fifty outbound requests.

    A separate endpoint from /competitors and /research on purpose. One
    combined investigation payload would make a caller that wants demand
    pay for everything else, and would grow without bound as phases are
    added.

    An investigation that has never run returns an empty-but-valid
    collection, which is a different fact from a run that searched and
    found nothing -- and the run endpoint is where that difference is
    readable.
    """
    if get_investigation(session, investigation_id=investigation_id) is None:
        raise AppError(
            f"investigation {investigation_id} not found", status_code=404
        )

    return get_demand_evidence(
        session, investigation_id=investigation_id, limit=limit
    )


@router.get("/{investigation_id}/competitors", response_model=CompetitorCollection)
def get_investigation_competitors(
    investigation_id: uuid.UUID,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=EVIDENCE_MAX_LIMIT)] = EVIDENCE_DEFAULT_LIMIT,
) -> CompetitorCollection:
    """Products discovered that address, or neighbour, this idea.

    Same discipline as /evidence: persisted rows only, no provider call,
    and no navigation into any competitor's site. There is no pricing and
    no feature list here because discovery never opened the page, and
    `name` is the page title rather than a company name this system
    invented.
    """
    if get_investigation(session, investigation_id=investigation_id) is None:
        raise AppError(
            f"investigation {investigation_id} not found", status_code=404
        )

    return get_competitors(session, investigation_id=investigation_id, limit=limit)
