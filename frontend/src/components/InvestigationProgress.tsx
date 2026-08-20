import type {
  InvestigationPhaseState,
  InvestigationRun,
  PlanningProgress,
  ResearchPhaseProgress,
  WebPhaseProgress,
} from "../api/investigationTypes";
import "./InvestigationProgress.css";

/**
 * What the run has actually done, phase by phase.
 *
 * EVERY NUMBER HERE COMES FROM THE BACKEND. There is no percentage, no
 * elapsed-time bar, and no interpolation between polls: a progress bar
 * that advances on a timer is indistinguishable from one that advances
 * on work, which is exactly how a 14-minute stall once looked identical
 * to a healthy run.
 *
 * Phase state is rendered from the six literal values the API returns.
 * `skipped` is deliberately its own thing -- a phase whose provider was
 * not configured produced no evidence AND no failure, and showing it as
 * either would be a lie in one direction or the other.
 */

const STATE_LABEL: Record<InvestigationPhaseState, string> = {
  pending: "Waiting",
  running: "Running",
  complete: "Complete",
  partial: "Partial",
  failed: "Failed",
  skipped: "Not run",
};

const STATE_MARK: Record<InvestigationPhaseState, string> = {
  pending: "○",
  running: "◐",
  complete: "✓",
  partial: "⚠",
  failed: "✕",
  skipped: "–",
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="ip-row">
      <span className="ip-row-label">{label}</span>
      <span className="ip-row-value">{value}</span>
    </div>
  );
}

function Phase({
  title,
  state,
  note,
  summary,
  children,
}: {
  title: string;
  state: InvestigationPhaseState;
  note?: string | null;
  summary: string;
  children?: React.ReactNode;
}) {
  return (
    <section className={`ip-phase is-${state}`}>
      <header className="ip-phase-head">
        <span className="ip-mark" aria-hidden="true">
          {STATE_MARK[state]}
        </span>
        <h4 className="ip-phase-title">{title}</h4>
        <span className="ip-state">{STATE_LABEL[state]}</span>
      </header>
      <p className="ip-summary">{summary}</p>
      {note ? <p className="ip-note">{note}</p> : null}
      {children ? (
        <details
          className="ip-details"
          open={state === "running" || state === "partial" || state === "failed"}
        >
          <summary>Measured counters</summary>
          <div className="ip-rows">{children}</div>
        </details>
      ) : null}
    </section>
  );
}

/** A partial phase is useful data plus a gap, stated rather than hidden. */
function webPhaseNote(phase: WebPhaseProgress, noun: string): string | null {
  if (phase.state === "partial") {
    return `${phase.queries_completed} of ${phase.queries_total} searches finished; ${phase.queries_succeeded} succeeded. Analysis used the evidence that returned.`;
  }
  if (phase.state === "failed") {
    return `No ${noun} search returned, so nothing was collected from this pass.`;
  }
  if (phase.state === "skipped") {
    return "Web discovery is not configured on this deployment.";
  }
  return null;
}

function classificationRows(
  phase: WebPhaseProgress,
  labels: Record<string, string>,
) {
  return Object.entries(labels)
    .filter(([key]) => phase.by_classification[key] !== undefined)
    .map(([key, label]) => (
      <Row key={key} label={label} value={String(phase.by_classification[key])} />
    ));
}

export function InvestigationProgress({ run }: { run: InvestigationRun }) {
  const planning: PlanningProgress = run.phases.planning;
  const research: ResearchPhaseProgress = run.phases.research;
  const demand: WebPhaseProgress = run.phases.demand;
  const competitors: WebPhaseProgress = run.phases.competitors;

  return (
    <div className="investigation-progress">
      <Phase
        title="Planning"
        state={planning.state}
        summary={`${planning.research_queries + planning.demand_queries + planning.competitor_queries} search directions`}
      >
        {planning.research_queries +
          planning.demand_queries +
          planning.competitor_queries >
        0 ? (
          <>
            <Row label="Research queries" value={String(planning.research_queries)} />
            <Row label="Demand queries" value={String(planning.demand_queries)} />
            <Row
              label="Competitor queries"
              value={String(planning.competitor_queries)}
            />
          </>
        ) : null}
      </Phase>

      <Phase
        title="Academic research"
        state={research.state}
        summary={`${research.matched} relevant paper${research.matched === 1 ? "" : "s"}`}
        note={
          research.state === "partial"
            ? `${research.queries_completed} of ${research.queries_total} searches returned. Results below come from those.`
            : null
        }
      >
        {research.queries_total > 0 ? (
          <>
            <Row
              label="Searches"
              value={`${research.queries_completed}/${research.queries_total}`}
            />
            <Row label="Papers discovered" value={`${research.discovered} unique`} />
            <Row
              label="Semantic review"
              value={`${research.judged}/${research.selected}`}
            />
            <Row label="Relevant papers" value={String(research.matched)} />
          </>
        ) : null}
      </Phase>

      <Phase
        title="Demand evidence"
        state={demand.state}
        summary={`${demand.accepted} accepted signal${demand.accepted === 1 ? "" : "s"}`}
        note={webPhaseNote(demand, "demand")}
      >
        {demand.queries_total > 0 ? (
          <>
            <Row
              label="Searches completed"
              value={`${demand.queries_completed}/${demand.queries_total}`}
            />
            <Row label="Searches succeeded" value={String(demand.queries_succeeded)} />
            <Row label="Candidates" value={`${demand.candidates} unique`} />
            <Row
              label="Evidence reviewed"
              value={`${demand.judged}/${demand.candidates}`}
            />
            <Row label="Accepted" value={String(demand.accepted)} />
            {classificationRows(demand, {
              strong_support: "Strong support",
              support: "Supporting",
              neutral: "Neutral",
              contradicts: "Contradicting",
              irrelevant: "Irrelevant",
            })}
          </>
        ) : null}
      </Phase>

      <Phase
        title="Competition"
        state={competitors.state}
        summary={`${competitors.accepted} candidate${competitors.accepted === 1 ? "" : "s"}`}
        note={webPhaseNote(competitors, "competitor")}
      >
        {competitors.queries_total > 0 ? (
          <>
            <Row
              label="Searches completed"
              value={`${competitors.queries_completed}/${competitors.queries_total}`}
            />
            <Row
              label="Searches succeeded"
              value={String(competitors.queries_succeeded)}
            />
            <Row label="Candidates" value={`${competitors.candidates} unique`} />
            <Row
              label="Reviewed"
              value={`${competitors.judged}/${competitors.candidates}`}
            />
            <Row label="Accepted" value={String(competitors.accepted)} />
            {classificationRows(competitors, {
              direct: "Direct candidates",
              adjacent: "Adjacent",
              substitute: "Substitutes",
              irrelevant: "Irrelevant",
            })}
          </>
        ) : null}
      </Phase>
    </div>
  );
}
