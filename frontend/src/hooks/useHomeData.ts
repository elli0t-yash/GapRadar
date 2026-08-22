import { useEffect, useState } from "react";
import { isAbort } from "../api/client";
import { industriesOf, toProblem } from "../api/adapters";
import { listOpportunities } from "../api/opportunities";
import { getLiveBrightDataEvidence } from "../api/reliability";
import {
  getInvestigationCompetitors,
  getInvestigationEvidence,
  getInvestigationResearch,
  listInvestigations,
} from "../api/investigations";
import type { LiveBrightDataEvidence } from "../api/types";
import type {
  CompetitorCollection,
  DemandEvidenceCollection,
  Investigation,
  InvestigationResearchIntelligence,
} from "../api/investigationTypes";
import type { Problem } from "../types";

/**
 * The landing page's data layer.
 *
 * EVERY call below is a GET against an endpoint the backend documents as a
 * pure read of persisted rows. Nothing here creates an investigation, starts
 * a run, requests enrichment, or advances the reliability demo -- the
 * landing page must never be able to spend provider budget, and the write
 * functions are deliberately not imported.
 *
 * Each hook reports failure as "we have nothing to show" rather than as an
 * error banner: on a marketing page an unreachable backend should quietly
 * collapse the section's live numbers, never break the narrative.
 */

export interface OpportunityFeed {
  problems: Problem[];
  markets: string[];
}

/** The ranked feed, loaded once. Order is the backend's; nothing re-ranks. */
export function useOpportunityFeed(): OpportunityFeed | null {
  const [feed, setFeed] = useState<OpportunityFeed | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    listOpportunities(200, controller.signal)
      .then((opportunities) => {
        const problems = opportunities.map(toProblem);
        setFeed({ problems, markets: industriesOf(problems) });
      })
      .catch((error) => {
        if (isAbort(error)) return;
        setFeed(null);
      });

    return () => controller.abort();
  }, []);

  return feed;
}

/**
 * Persisted proof from the isolated real Bright Data healing experiment.
 * Deferred until its section approaches the viewport.
 */
export function useLiveEvidence(enabled: boolean): LiveBrightDataEvidence | null {
  const [evidence, setEvidence] = useState<LiveBrightDataEvidence | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();

    getLiveBrightDataEvidence(controller.signal)
      .then(setEvidence)
      .catch((error) => {
        if (isAbort(error)) return;
        setEvidence(null);
      });

    return () => controller.abort();
  }, [enabled]);

  return evidence;
}

export interface InvestigationShowcase {
  investigation: Investigation;
  research: InvestigationResearchIntelligence;
  demand: DemandEvidenceCollection;
  competitors: CompetitorCollection;
}

export type ShowcaseState =
  | { status: "idle" }
  | { status: "loading" }
  /** Loaded, and there really is a completed investigation to show. */
  | { status: "ready"; showcase: InvestigationShowcase }
  /** Loaded, and this workspace has no completed investigation yet. */
  | { status: "empty" }
  /** The feed could not be read. Distinct from "there is nothing". */
  | { status: "unavailable" };

/** Most recently updated first, so the newest completed run is picked. */
function newestCompleted(investigations: Investigation[]): Investigation | null {
  const completed = investigations
    .filter((investigation) => investigation.status === "succeeded")
    .sort(
      (a, b) =>
        new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    );
  return completed[0] ?? null;
}

/**
 * One real, finished investigation with its three evidence families.
 *
 * Deferred until the section approaches the viewport: four reads is more
 * than a landing page should spend before the visitor has scrolled.
 */
export function useInvestigationShowcase(enabled: boolean): ShowcaseState {
  const [state, setState] = useState<ShowcaseState>({ status: "idle" });

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({ status: "loading" });

    listInvestigations(50, controller.signal)
      .then(async (investigations) => {
        const investigation = newestCompleted(investigations);
        if (!investigation) {
          setState({ status: "empty" });
          return;
        }

        const [research, demand, competitors] = await Promise.all([
          getInvestigationResearch(investigation.id, controller.signal),
          getInvestigationEvidence(investigation.id, controller.signal),
          getInvestigationCompetitors(investigation.id, controller.signal),
        ]);

        setState({
          status: "ready",
          showcase: { investigation, research, demand, competitors },
        });
      })
      .catch((error) => {
        if (isAbort(error)) return;
        setState({ status: "unavailable" });
      });

    return () => controller.abort();
  }, [enabled]);

  return state;
}
