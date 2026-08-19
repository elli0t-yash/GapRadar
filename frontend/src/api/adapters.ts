import type { Problem } from "../types";
import type { Opportunity } from "./types";

const SOURCE_LABELS: Record<string, string> = {
  fix_my_itch: "Fix My Itch",
};

/** "fix_my_itch" -> "Fix My Itch". Unknown slugs are title-cased, not hidden. */
function labelForSource(source: string | null): string | null {
  if (!source) return null;
  return (
    SOURCE_LABELS[source] ??
    source
      .split("_")
      .filter(Boolean)
      .map((word) => word[0].toUpperCase() + word.slice(1))
      .join(" ")
  );
}

/**
 * One API Opportunity -> one view model.
 *
 * Nothing is computed here. `opportunity_score` is rounded for display and
 * every null is carried through as a null -- the backend owns the score, and
 * a null means "not scorable", which is not zero.
 */
export function toProblem(opportunity: Opportunity): Problem {
  return {
    id: opportunity.id,
    // `title` and `problem` are the same string by contract; use one.
    title: opportunity.problem,
    description: opportunity.description,
    category: opportunity.industry,
    source: labelForSource(opportunity.source),
    sourceUrl: opportunity.source_url,
    signal:
      opportunity.opportunity_score === null
        ? null
        : Math.round(opportunity.opportunity_score),
    date: opportunity.observed_at,
    scores: {
      itch: opportunity.itch_score,
      severity: opportunity.severity_score,
      tam: opportunity.tam_score,
      whitespace: opportunity.whitespace_score,
      frequency: opportunity.frequency_score,
    },
  };
}

/**
 * The industry chips, derived from what the feed actually returned.
 *
 * Never a hard-coded list: the backend exposes no industry filter, so the
 * only honest source of chips is the data on screen.
 */
export function industriesOf(problems: Problem[]): string[] {
  return Array.from(
    new Set(
      problems
        .map((problem) => problem.category)
        .filter((category): category is string => Boolean(category)),
    ),
  ).sort();
}
