import raw from "./gapradar.json";
import type { Problem } from "../types";

interface RawProblem {
  problem: string;
  itch_score: number;
  industry: string;
  description: string;
  severity_score: number;
  tam_score: number;
  whitespace_score: number;
  frequency_score: number;
  source: string;
  source_url: string;
}

const SOURCE_LABELS: Record<string, string> = {
  fix_my_itch: "Fix My Itch",
};

function labelForSource(source: string): string {
  return (
    SOURCE_LABELS[source] ??
    source
      .split("_")
      .map((word) => word[0].toUpperCase() + word.slice(1))
      .join(" ")
  );
}

// The source data has no timestamp, so we derive a stable, deterministic
// pseudo-date per entry (spread across the last 90 days) purely so the UI
// has something meaningful to sort/display by. Swap for a real field once
// the API provides one.
function derivedDate(index: number): string {
  const daysAgo = (index * 7) % 90;
  const date = new Date();
  date.setDate(date.getDate() - daysAgo);
  return date.toISOString();
}

export const problems: Problem[] = (raw as RawProblem[]).map((entry, index) => ({
  id: `problem-${index}`,
  title: entry.problem,
  description: entry.description,
  category: entry.industry,
  source: labelForSource(entry.source),
  sourceUrl: entry.source_url,
  signal: Math.round(entry.itch_score),
  date: derivedDate(index),
  scores: {
    itch: entry.itch_score,
    severity: entry.severity_score,
    tam: entry.tam_score,
    whitespace: entry.whitespace_score,
    frequency: entry.frequency_score,
  },
}));

export const categories: string[] = Array.from(
  new Set(problems.map((p) => p.category)),
).sort();
