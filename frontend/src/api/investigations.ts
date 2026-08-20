import { apiGet, apiPost } from "./client";
import type {
  CompetitorCollection,
  DemandEvidenceCollection,
  Investigation,
  InvestigationCreate,
  InvestigationResearchIntelligence,
  InvestigationRun,
  InvestigationRunAccepted,
} from "./investigationTypes";

/**
 * The Investigation surface.
 *
 * EXACTLY TWO FUNCTIONS HERE WRITE ANYTHING, and only one of them can
 * cost money: `startInvestigationRun`. Every other call is a pure read of
 * persisted rows -- no search, no classification, and no fetching of the
 * URLs the evidence lists. That is a backend guarantee, and it is what
 * makes it safe to poll these and safe to open them on page load.
 */

/** Records an investigation. Deliberately does NOT start analysis. */
export function createInvestigation(
  input: InvestigationCreate,
  signal?: AbortSignal,
) {
  return apiPost<Investigation>("/investigations", { body: input, signal });
}

export function listInvestigations(limit?: number, signal?: AbortSignal) {
  return apiGet<Investigation[]>("/investigations", {
    params: { limit },
    signal,
  });
}

export function getInvestigation(id: string, signal?: AbortSignal) {
  return apiGet<Investigation>(`/investigations/${id}`, { signal });
}

/**
 * THE ONLY CALL IN THIS APPLICATION THAT CAN SPEND PROVIDER BUDGET.
 *
 * Must only ever be reached from an explicit user click. It is never
 * called from an effect, a route transition, or a render -- see
 * InvestigationDetailPage, which is its single call site.
 *
 * Answers 202 with `already_running` when a run is in flight, so a
 * double-click joins the existing run instead of buying a second one.
 */
export function startInvestigationRun(id: string, signal?: AbortSignal) {
  return apiPost<InvestigationRunAccepted>(`/investigations/${id}/run`, {
    signal,
  });
}

/** The latest run, or null when none has ever been requested. */
export function getInvestigationRun(id: string, signal?: AbortSignal) {
  return apiGet<InvestigationRun | null>(`/investigations/${id}/run`, {
    signal,
  });
}

export function getInvestigationResearch(id: string, signal?: AbortSignal) {
  return apiGet<InvestigationResearchIntelligence>(
    `/investigations/${id}/research`,
    { signal },
  );
}

export function getInvestigationEvidence(id: string, signal?: AbortSignal) {
  return apiGet<DemandEvidenceCollection>(`/investigations/${id}/evidence`, {
    signal,
  });
}

export function getInvestigationCompetitors(id: string, signal?: AbortSignal) {
  return apiGet<CompetitorCollection>(`/investigations/${id}/competitors`, {
    signal,
  });
}
