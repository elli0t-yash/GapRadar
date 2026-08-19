/** A card in the intro animation. Decorative chrome, not product data. */
export interface Listing {
  id: string;
  title: string;
  category: string;
  score: number;
}

/**
 * The view model the UI renders, adapted from the API's `Opportunity`
 * (see src/api/adapters.ts).
 *
 * Nullability mirrors the API exactly and is deliberate: the backend
 * distinguishes "not scorable" and "no industry" from zero and empty
 * string, and collapsing that here would render a null as a 0.
 */
export interface Problem {
  /** The API's `id` (signal_id) -- the key for detail and research. */
  id: string;
  title: string;
  description: string;
  /** `industry`. Null when the source published none. */
  category: string | null;
  /** Display label derived from the `source` slug. */
  source: string | null;
  sourceUrl: string | null;
  /** Math.round(opportunity_score). NULL = not scorable, never 0. */
  signal: number | null;
  /** `observed_at`, ISO-8601 with offset. */
  date: string;
  scores: {
    /** 0-100 scale. */
    itch: number | null;
    /** 1-10 scales. */
    severity: number | null;
    tam: number | null;
    whitespace: number | null;
    frequency: number | null;
  };
}
