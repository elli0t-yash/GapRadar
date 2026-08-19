// Product contracts are transcribed from docs/frontend-backend-integration.md
// §7 and §9. The isolated Reliability demo types below mirror its explicit
// backend response model. Field names, types and nullability match the API
// exactly — do not "tidy" them.

/** UUID, serialised as a string. */
export type UUID = string;
/** ISO-8601 with an explicit offset. Parse with new Date(); never slice. */
export type ISODateTime = string;
/** Calendar date, e.g. "2021-11-05". No time, no timezone. */
export type ISODate = string;

export type SignalType =
  | "complaint"
  | "question"
  | "feature_request"
  | "review"
  | "problem"
  | "research"
  | "other";

/** One trusted problem. §7. */
export interface Opportunity {
  id: UUID;
  source_id: UUID;
  collector_run_id: UUID;
  signal_type: SignalType;
  /** The source's problem text. Identical to `problem`. */
  title: string;
  problem: string;
  description: string;
  canonical_url: string;
  observed_at: ISODateTime;

  industry: string | null;
  /** 0-100 scale. */
  itch_score: number | null;
  /** 1-10 scale. */
  severity_score: number | null;
  /** 1-10 scale. A score, not a currency amount. */
  tam_score: number | null;
  /** 1-10 scale, may be fractional. */
  whitespace_score: number | null;
  /** 1-10 scale. */
  frequency_score: number | null;
  /** Slug, e.g. "fix_my_itch". */
  source: string | null;
  source_url: string | null;
  /** Derived 0-100, 2dp. null = NOT SCORABLE. Never treat as 0. */
  opportunity_score: number | null;
}

/** An arXiv subject category. `code` may be null. */
export interface ResearchCategory {
  code: string | null;
  label: string;
}

/** One accepted paper, with the verdict that admitted it. §9. */
export interface ResearchPaperMatch {
  research_paper_id: UUID;
  arxiv_id: string;
  title: string;
  /** Full abstract. */
  abstract: string;
  /** Card-sized excerpt, pre-truncated at a word boundary by the backend. */
  abstract_preview: string;
  authors: string[];
  categories: ResearchCategory[];
  published_at: ISODate;
  paper_url: string;
  pdf_url: string;
  /** 0-100. A backend SEMANTIC judgement — render it, never recompute it. */
  relevance_score: number;
  matched_concepts: string[];
  match_reason: string | null;
  /** 0-100 or null. null means NOT ASSESSED — never render as 0. */
  technical_readiness_score: number | null;
}

/** Everything persisted about the research behind one opportunity. §9. */
export interface ResearchIntelligence {
  signal_id: UUID;
  /** Empty means enrichment has never run for this opportunity. */
  generated_queries: string[];
  /** Candidate pool discovered, not the accepted set. */
  paper_count: number;
  /** Accepted total; may exceed top_papers.length. */
  matched_paper_count: number;
  /** null — never 0 — when there are no matches. */
  average_relevance_score: number | null;
  top_concepts: string[];
  /** Already ordered and already accepted. Do not sort, do not filter. */
  top_papers: ResearchPaperMatch[];
}

/** 404 / 500 bodies. */
export interface ApiErrorBody {
  detail: string;
}

/** 422 bodies use an ARRAY-valued detail — a different shape. */
export interface ValidationErrorBody {
  detail: { loc: (string | number)[]; msg: string; type: string }[];
}

export interface ReliabilityDemoFieldHealth {
  field: string;
  baseline_pct: number;
  current_pct: number;
  drop_pct: number | null;
  status: "healthy" | "drift" | "regression";
}

export interface ReliabilityDemoVerification {
  field: string;
  before_pct: number;
  after_pct: number;
  status: "pass" | "fail";
}

export interface ReliabilityDemoRepair {
  attempt: number;
  label: string;
  status: "healing" | "verifying" | "rejected" | "approved";
  changes: string[];
  verification: ReliabilityDemoVerification[];
}

export interface ReliabilityDemoTimelineEvent {
  at: ISODateTime;
  event: string;
  collector_run_id: UUID | null;
  attempt: number | null;
  detail: string | null;
}

export interface ReliabilityDemoProof {
  schema_fidelity: "PASS" | "FAIL";
  semantic_fidelity: "PASS" | "FAIL";
  source_fidelity: "PASS" | "FAIL";
  decision: "APPROVE" | "REJECT";
}

export interface ReliabilityDemo {
  scenario: string;
  mode: "fixture_replay";
  session_id: UUID | null;
  collector_id: UUID | null;
  collector_name: string;
  provider: string;
  external_collector_id: string;
  status:
    | "healthy"
    | "drift_detected"
    | "healing"
    | "verifying"
    | "rejected"
    | "self_healed";
  core_status: string;
  terminal: boolean;
  incident_id: UUID | null;
  classification: string | null;
  severity: string | null;
  confidence: number | null;
  recommended_action: string | null;
  affected_fields: string[];
  field_health: ReliabilityDemoFieldHealth[];
  repair_attempts: ReliabilityDemoRepair[];
  timeline: ReliabilityDemoTimelineEvent[];
  proof: ReliabilityDemoProof | null;
  started_at: ISODateTime | null;
  updated_at: ISODateTime | null;
}

export interface LiveEvidenceCollector {
  collector_id: UUID;
  name: string;
  provider: string;
  external_collector_id: string;
}

export interface LiveEvidenceRun {
  collector_run_id: UUID;
  provider_job_id: string;
  status: string;
  started_at: ISODateTime | null;
  completed_at: ISODateTime | null;
  fetched_record_count: number;
  valid_record_count: number;
  invalid_record_count: number;
  accepted_record_count: number;
}

export interface LiveEvidenceInvalidRecord {
  index: number | null;
  problem: string | null;
  field: string;
  value: number;
  allowed_min: number;
  allowed_max: number;
  reason: string;
  detail: string | null;
}

export interface LiveEvidenceDetection {
  incident_id: UUID;
  detected_at: ISODateTime;
  observed_record_count: number;
  field: string;
  classification: string;
  severity: string | null;
  confidence: number | null;
  recommended_action: string;
}

export interface LiveEvidenceRepairAttempt {
  attempt: number;
  status: string;
  provider_status: string | null;
  has_diff: boolean | null;
  preview_records: number | null;
  preview_valid_records: number | null;
  preview_invalid_records: number | null;
  deployed: boolean;
  patch_available: boolean;
  before_logic: string | null;
  after_logic: string | null;
  note: string | null;
}

export interface LiveEvidenceVerificationSample {
  problem: string;
  tam_score: number;
}

export interface LiveEvidenceFailedCheck {
  name: string;
  expected: string | null;
  observed: string | null;
  detail: string | null;
}

export interface LiveEvidenceVerification {
  run: LiveEvidenceRun;
  samples: LiveEvidenceVerificationSample[];
  contract_validation: string;
  regression_result: string;
  failed_checks: LiveEvidenceFailedCheck[];
  final_decision: string;
  final_status: string;
  recovery_proof: Record<string, unknown> | null;
}

export interface LiveEvidenceAutomationStage {
  stage: string;
  automation: string;
  result: string;
  detail: string;
}

export interface LiveBrightDataEvidence {
  available: boolean;
  mode: "persisted_real_brightdata_run";
  live_trigger_safe: boolean;
  live_trigger_reason: string;
  collector: LiveEvidenceCollector | null;
  broken_run: LiveEvidenceRun | null;
  invalid_records: LiveEvidenceInvalidRecord[];
  detection: LiveEvidenceDetection | null;
  repair_attempts: LiveEvidenceRepairAttempt[];
  repair_patch_available: boolean;
  repair_patch_note: string;
  verification: LiveEvidenceVerification | null;
  automation: LiveEvidenceAutomationStage[];
}

/** Lifecycle of one on-demand research enrichment job. */
export type ResearchEnrichmentStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

export const TERMINAL_ENRICHMENT_STATUSES: ResearchEnrichmentStatus[] = [
  "succeeded",
  "failed",
];

export function isEnrichmentTerminal(
  status: ResearchEnrichmentStatus,
): boolean {
  return TERMINAL_ENRICHMENT_STATUSES.includes(status);
}

/** 202 body of POST /opportunities/{signal_id}/research/enrich. */
export interface ResearchEnrichmentAccepted {
  enrichment_id: UUID;
  signal_id: UUID;
  status: ResearchEnrichmentStatus;
  /** Joined a job already in flight. Expected, never an error. */
  already_running: boolean;
  /** Research already exists; nothing was started. Just read it. */
  already_enriched: boolean;
}

/**
 * GET /opportunities/{signal_id}/research/enrichment.
 *
 * The endpoint returns null when analysis has never been requested, which
 * is a different fact from a job that exists and is queued.
 */
export interface ResearchEnrichmentRead {
  enrichment_id: UUID;
  signal_id: UUID;
  status: ResearchEnrichmentStatus;
  started_at: ISODateTime | null;
  completed_at: ISODateTime | null;
  /** Present only on "failed". Safe to show a user. */
  error: string | null;
  /**
   * A run that SUCCEEDED but is incomplete: some searches returned and
   * some timed out. Shown ALONGSIDE the research, never instead of it.
   */
  warning: string | null;
  /**
   * Per-query progress, in plan order.
   *
   * The ONLY permitted source for "2 of 3 searches complete". Progress
   * must never be animated on a timer here: a stuck provider job and a
   * healthy one would look identical, which is the exact failure this
   * field exists to make visible. Empty for runs that predate per-query
   * tracking, so treat it as optional detail rather than as status.
   */
  query_states: ResearchQueryState[];
}

/** Lifecycle of ONE research query inside an enrichment. */
export type ResearchQueryStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "timed_out";

export interface ResearchQueryState {
  query: string;
  status: ResearchQueryStatus;
  /** Bright Data's job id. An identifier, never a credential. */
  provider_job_id: string | null;
  records_received: number;
  papers_returned: number;
  error: string | null;
  elapsed_seconds: number | null;
}

const TERMINAL_QUERY_STATUSES: ResearchQueryStatus[] = [
  "succeeded",
  "failed",
  "timed_out",
];

/** How many of this run's searches have stopped, for any reason. */
export function completedSearchCount(
  states: readonly ResearchQueryState[],
): number {
  return states.filter((state) => TERMINAL_QUERY_STATUSES.includes(state.status))
    .length;
}

/** Searches that ended without returning papers. */
export function unfinishedSearchCount(
  states: readonly ResearchQueryState[],
): number {
  return states.filter(
    (state) => state.status === "timed_out" || state.status === "failed",
  ).length;
}

/** Papers acquired so far, across the searches that have returned. */
export function papersSoFar(states: readonly ResearchQueryState[]): number {
  return states.reduce((total, state) => total + state.papers_returned, 0);
}
