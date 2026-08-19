import { apiGet } from "./client";
import type { Opportunity, ResearchIntelligence, UUID } from "./types";

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
