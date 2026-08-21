import type { CSSProperties } from "react";
import { useInView } from "../../hooks/useInView";

const LANES = [
  {
    name: "Research",
    detail: "Academic work that already touches the problem.",
  },
  {
    name: "Demand",
    detail: "Pages that support, weaken, or contradict the hypothesis.",
  },
  {
    name: "Competition",
    detail: "Products already standing where the idea would stand.",
  },
];

/**
 * How an investigation is structured, as a diagram.
 *
 * No durations, no percentages, no progress theatre: the real per-phase
 * state lives on the investigation detail page, where it is read from the
 * run. This is a shape, and nothing here is presented as a measurement.
 */
export function InvestigationFlow() {
  const [ref, inView] = useInView<HTMLDivElement>();

  return (
    <section className="home-section home-flow" aria-labelledby="home-flow-title">
      <div className="container">
        <div className="home-section-head">
          <p className="home-eyebrow">How investigation works</p>
          <h2 id="home-flow-title" className="home-section-title">
            One hypothesis, three directions.
          </h2>
        </div>

        <div
          ref={ref}
          className={`flow-diagram${inView ? " is-in" : ""}`}
        >
          <p className="flow-node is-start">Your hypothesis</p>

          <span className="flow-connector is-trunk" aria-hidden="true" />

          <p className="flow-node is-planner">
            Investigation planner
            <span>Turns one sentence into separate search directions.</span>
          </p>

          <span className="flow-connector is-fan" aria-hidden="true" />

          <ul className="flow-lanes">
            {LANES.map((lane, index) => (
              <li key={lane.name} style={{ "--i": index } as CSSProperties}>
                <strong>{lane.name}</strong>
                <span>{lane.detail}</span>
              </li>
            ))}
          </ul>

          <span className="flow-connector is-merge" aria-hidden="true" />

          <p className="flow-node is-end">
            Evidence
            <span>Readable, sourced, and kept whether it agrees or not.</span>
          </p>
        </div>
      </div>
    </section>
  );
}
