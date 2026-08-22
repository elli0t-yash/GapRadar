import type { CSSProperties } from "react";
import { Reveal } from "./Reveal";
import type { OpportunityFeed } from "../../hooks/useHomeData";

/**
 * ILLUSTRATIVE CATEGORIES, not scraped text.
 *
 * The product does not publish "example signals" through any endpoint, and
 * inventing quotes with source attributions would be fabricating evidence.
 * These are the KINDS of thing the collectors read, labelled as such on
 * screen. The only real thing in this section is the opportunity at the end
 * of the funnel, which comes from the live feed.
 */
const SIGNAL_KINDS = [
  "Operational friction",
  "Repeated complaints",
  "Unmet workflow",
  "Recurring cost",
];

export function SignalConvergence({ feed }: { feed: OpportunityFeed | null }) {
  const strongest = feed?.problems[0] ?? null;

  return (
    <section className="home-section home-converge" aria-labelledby="home-converge-title">
      <div className="container">
        <Reveal className="home-section-head">
          <p className="home-eyebrow">Discovery Mode</p>
          <h2 id="home-converge-title" className="home-statement">
            The internet is full of problems.
            <span> Finding the valuable ones is harder.</span>
          </h2>
          <p className="home-lede">
            GapRadar reads market surfaces continuously, keeps only what
            survives reliability gating, and scores what is left as an
            opportunity rather than as another complaint.
          </p>
        </Reveal>

        <div className="converge-flow">
          <Reveal className="converge-stage is-signals">
            <p className="converge-label">Pain signals</p>
            <ul className="converge-signal-list">
              {SIGNAL_KINDS.map((kind, index) => (
                <li key={kind} style={{ "--i": index } as CSSProperties}>
                  {kind}
                </li>
              ))}
            </ul>
            <p className="converge-caption">
              Illustrative categories of signal, not scraped quotations.
            </p>
          </Reveal>

          <Reveal className="converge-stage is-core" delay={0.08}>
            <div className="converge-core">
              <span className="converge-core-mark" aria-hidden="true">
                GR
              </span>
              <p>GapRadar</p>
              <span className="converge-core-sub">
                Extraction · reliability gating · scoring
              </span>
            </div>
          </Reveal>

          <Reveal className="converge-stage is-output" delay={0.16}>
            <p className="converge-label">Ranked opportunity</p>
            {strongest ? (
              <article className="converge-output">
                <span className="converge-output-tag">
                  {strongest.category ?? "Uncategorised"}
                </span>
                <h3>{strongest.title}</h3>
                {strongest.signal !== null ? (
                  <p className="converge-output-score">
                    <strong>{strongest.signal}</strong>
                    <span>Opportunity score</span>
                  </p>
                ) : (
                  <p className="converge-output-score is-unscored">
                    <strong>Not scorable</strong>
                  </p>
                )}
                <p className="converge-output-note">
                  Live — currently the highest-ranked opportunity in the feed.
                </p>
              </article>
            ) : (
              <article className="converge-output is-placeholder" aria-hidden="true">
                <span className="converge-output-tag" />
                <span className="converge-output-line" />
                <span className="converge-output-line is-short" />
              </article>
            )}
          </Reveal>
        </div>
      </div>
    </section>
  );
}
