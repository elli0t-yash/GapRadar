// The Investigation contracts, kept apart from the opportunity ones.
//
// WHY A SEPARATE FILE. `ResearchIntelligence` in ./types.ts is the
// OPPORTUNITY surface's response and carries `signal_id`. The
// investigation surface returns `subject_id` + `origin` and no
// `signal_id` at all -- the backend deliberately froze the older
// endpoint's key set so it could not drift. Reusing that type here would
// make the compiler agree with a shape the server never sends.
//
// Every field name below is taken from the backend's OpenAPI schema.

/** Lifecycle of the investigation itself -- the durable subject. */
export type InvestigationStatus =
  | "draft"
  | "ready"
  | "running"
  | "succeeded"
  | "failed";

/** Lifecycle of ONE execution attempt against an investigation. */
export type InvestigationRunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

/**
 * Where one phase of a run has got to.
 *
 * `skipped` is a third thing distinct from complete and failed: a phase
 * whose provider was not configured produced no evidence AND no failure.
 */
export type InvestigationPhaseState =
  | "pending"
  | "running"
  | "complete"
  | "partial"
  | "failed"
  | "skipped";

export type DemandClassification =
  | "strong_support"
  | "support"
  | "neutral"
  | "contradicts"
  | "irrelevant";

export type CompetitorClassification =
  | "direct"
  | "adjacent"
  | "substitute"
  | "irrelevant";

/** A user-supplied hypothesis. NEVER a validated market signal. */
export interface Investigation {
  id: string;
  query: string;
  /** Null until something derives one. Nothing does yet. */
  title: string | null;
  description: string | null;
  industry: string | null;
  status: InvestigationStatus;
  created_at: string;
  updated_at: string;
}

export interface PlanningProgress {
  state: InvestigationPhaseState;
  research_queries: number;
  demand_queries: number;
  competitor_queries: number;
}

export interface ResearchPhaseProgress {
  state: InvestigationPhaseState;
  queries_total: number;
  queries_completed: number;
  /** Distinct papers acquired across all searches. */
  discovered: number;
  /** Survivors of the pre-filter and the candidate cap. */
  selected: number;
  /** Papers the semantic matcher actually returned on. */
  judged: number;
  /** Papers at or above the relevance threshold. */
  matched: number;
}

export interface WebPhaseProgress {
  state: InvestigationPhaseState;
  queries_total: number;
  queries_completed: number;
  queries_succeeded: number;
  /** DISTINCT urls across every successful search, never a per-query sum. */
  candidates: number;
  judged: number;
  /** Verdicts worth keeping -- excludes the ones judged irrelevant. */
  accepted: number;
  /** Counts keyed by this family's own classification vocabulary. */
  by_classification: Record<string, number>;
}

export interface InvestigationRunPhases {
  planning: PlanningProgress;
  research: ResearchPhaseProgress;
  demand: WebPhaseProgress;
  competitors: WebPhaseProgress;
}

export interface ResearchEnrichmentCounters {
  discovered: number;
  selected: number;
  judged: number;
  matched: number;
}

export type ResearchQueryStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "timed_out";

/** One academic search's observable state, for honest per-query progress. */
export interface WebSearchQueryState {
  query: string;
  status: ResearchQueryStatus;
  provider_job_id: string | null;
  records_received: number;
  papers_returned: number;
  error: string | null;
  elapsed_seconds: number | null;
}

export interface InvestigationRun {
  run_id: string;
  investigation_id: string;
  status: InvestigationRunStatus;
  started_at: string | null;
  completed_at: string | null;
  /** Present only on failure, written to be shown to a user. */
  error: string | null;
  /** Set ALONGSIDE a success, never instead of one. */
  warning: string | null;
  query_states: WebSearchQueryState[];
  outcome_reason: string | null;
  counters: ResearchEnrichmentCounters;
  phases: InvestigationRunPhases;
  research_queries_total: number;
  research_queries_completed: number;
  /**
   * Whether offering a retry could plausibly change the outcome.
   *
   * COMPUTED BY THE BACKEND from its outcome taxonomy. The browser must
   * never re-derive this from `status`, `error` or `outcome_reason` --
   * a second taxonomy would drift the moment a reason is added.
   */
  is_retryable: boolean;
  /** The authoritative stop condition for polling. */
  is_terminal: boolean;
}

/** The 202 answer to "please investigate this". */
export interface InvestigationRunAccepted {
  run_id: string;
  investigation_id: string;
  status: InvestigationRunStatus;
  /** True when this request joined a run already in flight. */
  already_running: boolean;
}

export interface InvestigationPaperMatch {
  research_paper_id: string;
  arxiv_id: string;
  title: string;
  abstract: string;
  abstract_preview: string;
  authors: string[];
  categories: Array<{ code: string | null; label: string }>;
  published_at: string;
  paper_url: string;
  pdf_url: string;
  relevance_score: number;
  matched_concepts: string[];
  match_reason: string | null;
  /** Null means "not assessed", never "not ready". */
  technical_readiness_score: number | null;
}

/**
 * The INVESTIGATION research shape. Note `subject_id`/`origin` and the
 * absence of `signal_id` -- see the note at the top of this file.
 */
export interface InvestigationResearchIntelligence {
  subject_id: string;
  origin: "signal" | "investigation";
  generated_queries: string[];
  /** The candidate pool discovered, not the accepted set. */
  paper_count: number;
  matched_paper_count: number;
  /** Null -- never 0 -- when there are no matches. */
  average_relevance_score: number | null;
  top_concepts: string[];
  top_papers: InvestigationPaperMatch[];
}

/** How GapRadar came to be looking at one page. */
export interface WebEvidenceProvenance {
  found_by_queries: string[];
  best_position: number | null;
}

export interface DemandEvidence {
  id: string;
  url: string;
  domain: string;
  title: string;
  snippet: string;
  /** Only when the provider stated a reliable absolute date. */
  published_at: string | null;
  classification: DemandClassification;
  relevance_score: number;
  reason: string;
  provenance: WebEvidenceProvenance;
}

export interface DemandEvidenceCollection {
  investigation_id: string;
  /** Counts by classification value, including the ones that disagree. */
  counts: Record<string, number>;
  evidence: DemandEvidence[];
}

export interface CompetitorEvidence {
  id: string;
  url: string;
  domain: string;
  /** A DISPLAY IDENTITY -- the page title, not a verified company name. */
  name: string;
  snippet: string;
  classification: CompetitorClassification;
  relevance_score: number;
  reason: string;
  provenance: WebEvidenceProvenance;
}

export interface CompetitorCollection {
  investigation_id: string;
  counts: Record<string, number>;
  competitors: CompetitorEvidence[];
}

export interface InvestigationCreate {
  query: string;
  industry?: string;
}
