// Transcribed from docs/frontend-backend-integration.md §7 and §9, which are
// generated from the backend's OpenAPI document. Field names, types and
// nullability match the API exactly — do not "tidy" them.

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
