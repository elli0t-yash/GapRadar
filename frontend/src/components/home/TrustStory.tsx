import { Link } from "react-router-dom";
import { Reveal } from "./Reveal";
import { useInView } from "../../hooks/useInView";
import { useLiveEvidence } from "../../hooks/useHomeData";

/** The lifecycle, as a diagram. Labelled on screen as an illustration. */
const FLOW = [
  { step: "Source extraction", note: "A collector returns records." },
  { step: "Extraction quality changes", note: "The page or the scraper moves." },
  { step: "Extraction drift detected", note: "RecallGuard opens an incident." },
  { step: "Validation", note: "Schema · Semantics · Source fidelity." },
  { step: "Approve or reject", note: "Only a verified repair is trusted." },
];

const DIMENSIONS = [
  {
    name: "Schema",
    detail: "Are the fields still the fields the contract promised?",
  },
  {
    name: "Semantics",
    detail: "Do the values still mean what the field says they mean?",
  },
  {
    name: "Source fidelity",
    detail: "Does the record still match what the source actually published?",
  },
];

function titleCase(value: string | null): string {
  if (!value) return "—";
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function TrustStory() {
  const [sectionRef, near] = useInView<HTMLElement>("0px 0px 25% 0px");
  const evidence = useLiveEvidence(near);

  const collector = evidence?.collector ?? null;
  const brokenRun = evidence?.broken_run ?? null;
  const detection = evidence?.detection ?? null;
  const verification = evidence?.verification ?? null;
  const firstInvalid = evidence?.invalid_records[0] ?? null;
  const hasLiveProof = Boolean(
    evidence?.available && collector && brokenRun && detection,
  );

  return (
    <section
      ref={sectionRef}
      className="home-section home-trust"
      data-surface="dark"
      aria-labelledby="home-trust-title"
    >
      <div className="container">
        <Reveal className="home-section-head">
          <p className="home-eyebrow">RecallGuard</p>
          <h2 id="home-trust-title" className="home-section-title">
            Bad extraction creates
            <span> bad intelligence.</span>
          </h2>
          <p className="home-lede">
            GapRadar checks whether evidence can be trusted before unreliable
            extraction influences opportunity intelligence. A scraper that
            quietly starts returning the wrong thing is the failure mode that
            poisons everything downstream.
          </p>
        </Reveal>

        <Reveal className="trust-flow" delay={0.06}>
          <ol>
            {FLOW.map((stage, index) => (
              <li key={stage.step}>
                <span className="trust-flow-index" aria-hidden="true">
                  {index + 1}
                </span>
                <strong>{stage.step}</strong>
                <span className="trust-flow-note">{stage.note}</span>
              </li>
            ))}
          </ol>
          <p className="trust-flow-caption">
            Diagram of the RecallGuard lifecycle. Illustrative — the measured
            evidence is below.
          </p>
        </Reveal>

        <div className="trust-proofs">
          <Reveal className="trust-proof is-live">
            <header>
              <p className="trust-proof-kicker">Historical live provider run</p>
              <h3>Real Bright Data extraction, rejection retained.</h3>
              <p className="trust-proof-badges">
                <span>Real Bright Data</span>
                <span>Historical</span>
                <span>Read only</span>
              </p>
            </header>

            {hasLiveProof && brokenRun && detection && collector ? (
              <>
                <dl className="trust-counts">
                  <div>
                    <dt>Records fetched</dt>
                    <dd>{brokenRun.fetched_record_count}</dd>
                  </div>
                  <div>
                    <dt>Passed validation</dt>
                    <dd className="is-bad">{brokenRun.valid_record_count}</dd>
                  </div>
                  <div>
                    <dt>Rejected</dt>
                    <dd className="is-bad">{brokenRun.invalid_record_count}</dd>
                  </div>
                  <div>
                    <dt>Reached intelligence</dt>
                    <dd>{brokenRun.accepted_record_count}</dd>
                  </div>
                </dl>

                {firstInvalid ? (
                  <p className="trust-invalid">
                    <span className="trust-invalid-label">What broke</span>
                    <code>{firstInvalid.field}</code> came back as{" "}
                    <strong>{firstInvalid.value}</strong> where the contract
                    allows {firstInvalid.allowed_min}–{firstInvalid.allowed_max}.{" "}
                    {titleCase(firstInvalid.reason)}.
                  </p>
                ) : null}

                <dl className="trust-facts">
                  <div>
                    <dt>Collector</dt>
                    <dd>{collector.name}</dd>
                  </div>
                  <div>
                    <dt>Detection</dt>
                    <dd>{titleCase(detection.classification)}</dd>
                  </div>
                  <div>
                    <dt>Severity</dt>
                    <dd>
                      {detection.severity
                        ? titleCase(detection.severity)
                        : "Not persisted"}
                    </dd>
                  </div>
                  <div>
                    <dt>Confidence</dt>
                    <dd>
                      {detection.confidence == null
                        ? "Not persisted"
                        : `${(detection.confidence * 100).toFixed(1)}%`}
                    </dd>
                  </div>
                </dl>

                {verification ? (
                  <p
                    className={`trust-decision is-${verification.final_decision.toLowerCase()}`}
                  >
                    <span>Final decision</span>
                    <strong>{verification.final_decision}</strong>
                    <em>
                      Contract validation {verification.contract_validation} ·
                      regression guard {verification.regression_result}
                    </em>
                  </p>
                ) : null}

                <p className="trust-honesty">
                  This run proves detection and a safety rejection on a real
                  provider. It is not a claim of proven autonomous self-healing
                  against live sites.
                </p>
              </>
            ) : (
              <p className="trust-unavailable">
                {evidence
                  ? (evidence.live_trigger_reason ??
                    "Provider evidence is not registered in this database.")
                  : "Loading the persisted provider evidence…"}
              </p>
            )}
          </Reveal>

          <Reveal className="trust-proof is-fixture" delay={0.08}>
            <header>
              <p className="trust-proof-kicker">Deterministic fixture replay</p>
              <h3>The lifecycle, reproducible on demand.</h3>
              <p className="trust-proof-badges">
                <span>Fixture replay</span>
                <span>No provider call</span>
              </p>
            </header>

            <p className="trust-proof-copy">
              The approve/reject lifecycle replays step by step from a persisted
              fixture against an isolated development collector, so the same
              decision can be reproduced without touching a provider. Every
              repair has to clear three checks before anything is trusted.
            </p>

            <ul className="trust-dimensions">
              {DIMENSIONS.map((dimension) => (
                <li key={dimension.name}>
                  <strong>{dimension.name}</strong>
                  <span>{dimension.detail}</span>
                </li>
              ))}
            </ul>

            <Link className="trust-proof-link" to="/reliability">
              Run the reliability evidence <span aria-hidden="true">→</span>
            </Link>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
