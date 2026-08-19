import { apiGet, apiPost } from "./client";
import type {
  Opportunity,
  ResearchEnrichmentAccepted,
  ResearchEnrichmentRead,
  ResearchIntelligence,
  UUID,
} from "./types";

/**
 * The Discover feed. 200 is the backend's hard maximum and returns the whole
 * corpus today, so the UI loads it once and filters in memory (§15).
 *
 * The array arrives ALREADY RANKED best-first. Preserve that order.
 */
export function listOpportunities(
  limit = 200,
  signal?: AbortSignal,
): Promise<Opportunity[]> {
  return apiGet<Opportunity[]>("/opportunities", { params: { limit }, signal });
}

/**
 * One opportunity, authoritative.
 *
 * 404 means "not available" and deliberately does not distinguish "no such
 * signal" from "its collector is not currently trusted".
 */
export function getOpportunity(
  signalId: UUID,
  signal?: AbortSignal,
): Promise<Opportunity> {
  return apiGet<Opportunity>(`/opportunities/${encodeURIComponent(signalId)}`, {
    signal,
  });
}

/**
 * Persisted research intelligence for one opportunity.
 *
 * READ ONLY on the backend: this triggers no acquisition and no semantic
 * matching. An opportunity that has never been enriched answers 200 with
 * empty fields rather than 404 -- see §9's five states.
 */
export function getOpportunityResearch(
  signalId: UUID,
  signal?: AbortSignal,
): Promise<ResearchIntelligence> {
  return apiGet<ResearchIntelligence>(
    `/opportunities/${encodeURIComponent(signalId)}/research`,
    { signal },
  );
}

/**
 * Ask the backend to find research for one opportunity.
 *
 * THE ONLY CALL THAT SPENDS A PROVIDER RUN, and it must only ever be made
 * from an explicit user action -- never from an effect, never on mount.
 * Returns 202 immediately; the work happens server-side.
 *
 * `already_running` and `already_enriched` are both SUCCESS answers.
 */
export function startResearchEnrichment(
  signalId: UUID,
  signal?: AbortSignal,
): Promise<ResearchEnrichmentAccepted> {
  return apiPost<ResearchEnrichmentAccepted>(
    `/opportunities/${encodeURIComponent(signalId)}/research/enrich`,
    { signal },
  );
}

/**
 * Where this opportunity's most recent analysis has got to.
 *
 * Read-only and safe to poll. Resolves to null when analysis has never
 * been requested.
 */
export function getResearchEnrichment(
  signalId: UUID,
  signal?: AbortSignal,
): Promise<ResearchEnrichmentRead | null> {
  return apiGet<ResearchEnrichmentRead | null>(
    `/opportunities/${encodeURIComponent(signalId)}/research/enrichment`,
    { signal },
  );
}
