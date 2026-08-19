import { apiGet, apiPost } from "./client";
import type { LiveBrightDataEvidence, ReliabilityDemo } from "./types";

export function getLiveBrightDataEvidence(signal?: AbortSignal) {
  return apiGet<LiveBrightDataEvidence>("/reliability/live-evidence", { signal });
}

export function getReliabilityDemo(signal?: AbortSignal) {
  return apiGet<ReliabilityDemo>("/reliability/demo", { signal });
}

export function startReliabilityDemo(signal?: AbortSignal) {
  return apiPost<ReliabilityDemo>("/reliability/demo/start", { signal });
}

export function advanceReliabilityDemo(signal?: AbortSignal) {
  return apiPost<ReliabilityDemo>("/reliability/demo/advance", { signal });
}
