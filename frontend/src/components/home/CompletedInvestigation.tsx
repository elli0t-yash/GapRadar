import { Link } from "react-router-dom";
import { Reveal } from "./Reveal";
import { useInView } from "../../hooks/useInView";
import { useInvestigationShowcase } from "../../hooks/useHomeData";
import type {
  CompetitorEvidence,
  DemandClassification,
  DemandEvidence,
} from "../../api/investigationTypes";

const DEMAND_LABELS: Record<DemandClassification, string> = {
  strong_support: "Strong support",
  support: "Support",
  neutral: "Neutral",
  contradicts: "Contradicts",
  irrelevant: "Irrelevant",
};

/**
 * Contradicting evidence is picked FIRST when it exists.
 *
 * The product's whole claim is that it keeps evidence that disagrees, so the
 * showcase must not quietly prefer the flattering item.
 */
const DEMAND_PRIORITY: DemandClassification[] = [
  "contradicts",
  "strong_support",
  "support",
  "neutral",
];

function pickDemand(evidence: DemandEvidence[]): DemandEvidence | null {
  for (const classification of DEMAND_PRIORITY) {
    const match = evidence.find((item) => item.classification === classification);
    if (match) return match;
  }
  return evidence[0] ?? null;
}

function pickCompetitor(
  competitors: CompetitorEvidence[],
): CompetitorEvidence | null {
  const order = ["direct", "adjacent", "substitute"];
  for (const classification of order) {
    const match = competitors.find(
      (item) => item.classification === classification,
    );
    if (match) return match;
  }
  return competitors[0] ?? null;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function CompletedInvestigation() {
  const [sectionRef, near] = useInView<HTMLElement>("0px 0px 25% 0px");
  const state = useInvestigationShowcase(near);

  const showcase = state.status === "ready" ? state.showcase : null;
  const paper = showcase?.research.top_papers[0] ?? null;
  const demand = showcase ? pickDemand(showcase.demand.evidence) : null;
  const competitor = showcase
    ? pickCompetitor(showcase.competitors.competitors)
    : null;

  return (
    <section
      ref={sectionRef}
      className="home-section home-result"
      aria-labelledby="home-result-title"
    >
      <div className="container">
        <Reveal className="home-section-head">
          <p className="home-eyebrow">Investigation complete</p>
          <h2 id="home-result-title" className="home-section-title">
            Evidence, not verdicts.
          </h2>
          <p className="home-lede">
            GapRadar does not tell you to build it. It shows you what it found,
            where each piece came from, and why it was kept — including the
            evidence that argues against the idea.
          </p>
        </Reveal>

        {showcase ? (
          <>
            <Reveal className="result-slab" delay={0.06}>
              <p className="result-kicker">
                Completed investigation in this workspace
              </p>
              <h3 className="result-query">{showcase.investigation.query}</h3>

              <dl className="result-counts">
                <div>
                  <dt>Academic research</dt>
                  <dd>
                    {plural(
                      showcase.research.matched_paper_count,
                      "relevant paper",
                      "relevant papers",
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Demand evidence</dt>
                  <dd>
                    {plural(
                      showcase.demand.evidence.length,
                      "accepted item",
                      "accepted items",
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Competitor candidates</dt>
                  <dd>
                    {plural(
                      showcase.competitors.competitors.length,
                      "candidate",
                      "candidates",
                    )}
                  </dd>
                </div>
              </dl>

              <Link
                className="result-link"
                to={`/investigations/${showcase.investigation.id}`}
              >
                Open the full evidence workspace{" "}
                <span aria-hidden="true">→</span>
              </Link>
            </Reveal>

            <div className="result-panels">
              {paper ? (
                <Reveal className="result-panel">
                  <p className="result-panel-kicker">Research</p>
                  <h4>{paper.title}</h4>
                  {paper.match_reason ? (
                    <div className="result-why">
                      <strong>Why GapRadar matched it</strong>
                      <p>{paper.match_reason}</p>
                    </div>
                  ) : null}
                  {paper.matched_concepts.length > 0 ? (
                    <ul className="result-chips">
                      {paper.matched_concepts.slice(0, 4).map((concept) => (
                        <li key={concept}>{concept}</li>
                      ))}
                    </ul>
                  ) : null}
                  <a href={paper.paper_url} target="_blank" rel="noreferrer">
                    View paper <span aria-hidden="true">↗</span>
                  </a>
                </Reveal>
              ) : null}

              {demand ? (
                <Reveal className="result-panel is-demand" delay={0.06}>
                  <p className="result-panel-kicker">
                    {demand.classification === "contradicts"
                      ? "Evidence against the hypothesis"
                      : "Demand"}
                  </p>
                  <p
                    className={`result-classification is-${demand.classification}`}
                  >
                    {DEMAND_LABELS[demand.classification]}
                  </p>
                  <h4>{demand.title}</h4>
                  <p className="result-domain">{demand.domain}</p>
                  <p className="result-snippet">{demand.snippet}</p>
                  <div className="result-why">
                    <strong>Why GapRadar included this</strong>
                    <p>{demand.reason}</p>
                  </div>
                  <p className="result-provenance">
                    Found across{" "}
                    {plural(
                      demand.provenance.found_by_queries.length,
                      "search direction",
                      "search directions",
                    )}
                  </p>
                  <a href={demand.url} target="_blank" rel="noreferrer">
                    View source <span aria-hidden="true">↗</span>
                  </a>
                </Reveal>
              ) : null}

              {competitor ? (
                <Reveal className="result-panel" delay={0.12}>
                  <p className="result-panel-kicker">Competition</p>
                  <p
                    className={`result-classification is-${competitor.classification}`}
                  >
                    {competitor.classification}
                  </p>
                  <h4>{competitor.name}</h4>
                  <p className="result-domain">{competitor.domain}</p>
                  <div className="result-why">
                    <strong>Why GapRadar classified this</strong>
                    <p>{competitor.reason}</p>
                  </div>
                  <a href={competitor.url} target="_blank" rel="noreferrer">
                    View source <span aria-hidden="true">↗</span>
                  </a>
                </Reveal>
              ) : null}
            </div>
          </>
        ) : (
          <Reveal className="result-slab is-empty" delay={0.06}>
            <p className="result-kicker">Completed investigation</p>
            <h3 className="result-query">
              {state.status === "unavailable"
                ? "The investigation workspace could not be read from here."
                : state.status === "empty"
                  ? "No completed investigation in this workspace yet."
                  : "Loading the most recent completed investigation…"}
            </h3>
            <p className="result-empty-note">
              This panel only ever shows a real, finished investigation. Start
              one and its research, demand evidence, and competitor candidates
              appear here.
            </p>
            <Link className="result-link" to="/investigate">
              Start an investigation <span aria-hidden="true">→</span>
            </Link>
          </Reveal>
        )}
      </div>
    </section>
  );
}
