import { useCallback, useEffect, useState } from "react";
import {
  advanceReliabilityDemo,
  getLiveBrightDataEvidence,
  getReliabilityDemo,
  startReliabilityDemo,
} from "../api/reliability";
import { ApiError, isAbort, NetworkError } from "../api/client";
import type {
  LiveBrightDataEvidence,
  ReliabilityDemo,
  ReliabilityDemoTimelineEvent,
} from "../api/types";
import { PageShell } from "../components/PageShell";
import "./ReliabilityPage.css";

const STEP_DELAY_MS = 900;

const STATUS_LABELS: Record<ReliabilityDemo["status"], string> = {
  healthy: "Healthy",
  drift_detected: "Drift detected",
  healing: "Healing",
  verifying: "Verifying",
  rejected: "Repair rejected",
  self_healed: "Self-healed",
};

const EVENT_LABELS: Record<string, string> = {
  collector_healthy: "Scraper healthy",
  detected: "Extraction regression detected",
  healing_started: "Repair attempted",
  repair_candidate_registered: "Repair candidate registered",
  verification_started: "Repair verification started",
  healing_failed: "Regression guard rejected repair",
  verification_passed: "Good repair approved — self-healed",
};

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "We couldn't connect to GapRadar. Retained reliability evidence remains safe.";
  }
  if (error instanceof ApiError && error.status >= 500) {
    return "GapRadar couldn't load the reliability evidence just now.";
  }
  return error instanceof Error ? error.message : "The reliability demo failed.";
}

function label(value: string | null): string {
  if (!value) return "—";
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function timelineDetail(event: ReliabilityDemoTimelineEvent): string | null {
  if (event.event === "detected" && event.detail) return label(event.detail);
  if (event.event === "healing_started" && event.attempt) {
    return `Attempt ${event.attempt}`;
  }
  if (event.event === "healing_failed") return "Healthy title coverage regressed";
  return event.detail ? label(event.detail) : null;
}

function LiveEvidenceSection({
  evidence,
}: {
  evidence: LiveBrightDataEvidence | null;
}) {
  const collector = evidence?.collector;
  const brokenRun = evidence?.broken_run;
  const detection = evidence?.detection;

  return (
    <section className="live-evidence-section" aria-labelledby="live-evidence-title">
      <header className="live-evidence-header">
        <div>
          <p className="reliability-eyebrow">Live Bright Data evidence</p>
          <h2 id="live-evidence-title">A real provider run, with the rejection retained.</h2>
          <p>
            Persisted evidence from the isolated Scraper Studio experiment—not
            the fixture replay below.
          </p>
        </div>
        <div className="live-evidence-badges" aria-label="Evidence provenance">
          <span>Real Bright Data</span>
          <span>Historical run</span>
          <span>Read only</span>
        </div>
      </header>

      {!evidence || !evidence.available || !collector || !brokenRun || !detection ? (
        <div className="reliability-card live-evidence-unavailable">
          <strong>Provider evidence is not available in this database.</strong>
          <p>{evidence?.live_trigger_reason ?? "The evidence endpoint has not loaded."}</p>
        </div>
      ) : (
        <>
          <div className="live-evidence-safety">
            <strong>Fresh live trigger disabled.</strong>
            <span>{evidence.live_trigger_reason}</span>
          </div>

          <div className="reliability-grid live-evidence-summary">
            <article className="reliability-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Collector</span>
                  <h3>{collector.name}</h3>
                </div>
                <span className="live-provider-mark">Bright Data</span>
              </div>
              <dl className="live-evidence-facts">
                <div>
                  <dt>Scraper Studio ID</dt>
                  <dd><code>{collector.external_collector_id}</code></dd>
                </div>
                <div>
                  <dt>GapRadar collector run</dt>
                  <dd><code>{brokenRun.collector_run_id}</code></dd>
                </div>
                <div>
                  <dt>Provider job ID</dt>
                  <dd><code>{brokenRun.provider_job_id}</code></dd>
                </div>
                <div>
                  <dt>Actual job status</dt>
                  <dd><span className="evidence-fail">{label(brokenRun.status)}</span></dd>
                </div>
                <div>
                  <dt>Started</dt>
                  <dd>{formatDateTime(brokenRun.started_at)}</dd>
                </div>
                <div>
                  <dt>Records</dt>
                  <dd>{brokenRun.fetched_record_count} fetched · {brokenRun.invalid_record_count} invalid</dd>
                </div>
              </dl>
            </article>

            <article className="reliability-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Detection</span>
                  <h3>{label(detection.classification)}</h3>
                </div>
                <span className="reliability-severity">Request heal</span>
              </div>
              <dl className="live-evidence-facts">
                <div>
                  <dt>Incident ID</dt>
                  <dd><code>{detection.incident_id}</code></dd>
                </div>
                <div>
                  <dt>Observed records</dt>
                  <dd>{detection.observed_record_count}</dd>
                </div>
                <div>
                  <dt>Affected field</dt>
                  <dd><code>{detection.field}</code></dd>
                </div>
                <div>
                  <dt>Recommended action</dt>
                  <dd>{label(detection.recommended_action)}</dd>
                </div>
                <div>
                  <dt>Severity</dt>
                  <dd>{detection.severity ? label(detection.severity) : "Not persisted"}</dd>
                </div>
                <div>
                  <dt>Confidence</dt>
                  <dd>{detection.confidence == null ? "Not persisted" : `${(detection.confidence * 100).toFixed(1)}%`}</dd>
                </div>
              </dl>
            </article>
          </div>

          <article className="reliability-card live-invalid-card">
            <div className="reliability-card-heading">
              <div>
                <span className="reliability-kicker">Broken run</span>
                <h3>Actual invalid-score evidence</h3>
              </div>
              <span className="evidence-count">{brokenRun.valid_record_count} valid / {brokenRun.invalid_record_count} invalid</span>
            </div>
            <div className="reliability-table-wrap">
              <table className="live-evidence-table">
                <thead>
                  <tr>
                    <th>Record</th>
                    <th>Field</th>
                    <th>Before</th>
                    <th>Allowed</th>
                    <th>Validation</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.invalid_records.map((record) => (
                    <tr key={`${record.index}-${record.problem}`}>
                      <td>{record.problem ?? `Record ${record.index ?? "—"}`}</td>
                      <td><code>{record.field}</code></td>
                      <td><strong className="evidence-fail">{record.value}</strong></td>
                      <td>{record.allowed_min}–{record.allowed_max}</td>
                      <td><span className="field-status field-status--regression">{label(record.reason)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>

          <div className="reliability-grid live-repair-grid">
            <article className="reliability-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Real repair history</span>
                  <h3>Provider candidates &amp; deploy gate</h3>
                </div>
              </div>
              <div className="live-repair-list">
                {evidence.repair_attempts.map((attempt) => (
                  <div key={attempt.attempt}>
                    <header>
                      <strong>Attempt {attempt.attempt}</strong>
                      <span>{label(attempt.status)}</span>
                    </header>
                    <p>
                      Provider: {label(attempt.provider_status)} · Preview: {attempt.preview_records ?? "not retained"}
                      {attempt.preview_valid_records != null ? ` (${attempt.preview_valid_records} valid, ${attempt.preview_invalid_records ?? 0} invalid)` : ""}
                    </p>
                    {attempt.note && <small>{attempt.note}</small>}
                  </div>
                ))}
              </div>
              <div className="patch-unavailable">
                <strong>Patch body unavailable</strong>
                <p>{evidence.repair_patch_note}</p>
              </div>
            </article>

            <article className="reliability-card live-verification-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Fresh verification run</span>
                  <h3>{evidence.verification ? evidence.verification.run.provider_job_id : "Not persisted"}</h3>
                </div>
              </div>
              {evidence.verification ? (
                <>
                  <dl className="live-verdicts">
                    <div><dt>Provider status</dt><dd>{label(evidence.verification.run.status)}</dd></div>
                    <div><dt>Fresh records</dt><dd>{evidence.verification.run.accepted_record_count}</dd></div>
                    <div><dt>Score contract</dt><dd className="evidence-pass">{evidence.verification.contract_validation}</dd></div>
                    <div><dt>Regression guard</dt><dd className="evidence-fail">{evidence.verification.regression_result}</dd></div>
                  </dl>
                  <div className="verification-samples">
                    {evidence.verification.samples.map((sample) => (
                      <div key={sample.problem}>
                        <span>{sample.problem}</span>
                        <code>tam_score {sample.tam_score}</code>
                      </div>
                    ))}
                  </div>
                  {evidence.verification.failed_checks.map((check) => (
                    <div className="regression-proof" key={check.name}>
                      <strong>{label(check.name)} failed</strong>
                      <span>{check.expected}</span>
                      <span>{check.observed}</span>
                    </div>
                  ))}
                  <div className="live-final-decision">
                    <span>Final RecallGuard decision</span>
                    <strong>{evidence.verification.final_decision} · {label(evidence.verification.final_status)}</strong>
                    <small>Recovery proof: {evidence.verification.recovery_proof ? "Persisted" : "Not produced"}</small>
                  </div>
                </>
              ) : (
                <p className="reliability-placeholder">No fresh provider verification run was persisted.</p>
              )}
            </article>
          </div>

          <article className="reliability-card automation-card">
            <div className="reliability-card-heading">
              <div>
                <span className="reliability-kicker">Automation audit</span>
                <h3>What the historical path actually automated</h3>
              </div>
            </div>
            <div className="automation-flow">
              {evidence.automation.map((stage) => (
                <div key={stage.stage}>
                  <span>{stage.stage}</span>
                  <strong>{stage.result}</strong>
                  <small>{stage.automation}</small>
                  <p>{stage.detail}</p>
                </div>
              ))}
            </div>
          </article>
        </>
      )}
    </section>
  );
}

function ReliabilityNarrative({ demo }: { demo: ReliabilityDemo | null }) {
  const hazard = demo?.field_health.find((field) => field.field === "hazard");
  const approvedRepair = demo?.repair_attempts.find(
    (repair) => repair.status === "approved",
  );
  const hazardValidation = approvedRepair?.verification.find(
    (check) => check.field === "hazard",
  );
  const driftValue =
    hazardValidation?.before_pct ??
    demo?.repair_attempts
      .flatMap((repair) => repair.verification)
      .find((check) => check.field === "hazard")?.before_pct ??
    hazard?.current_pct;

  return (
    <section className="reliability-narrative" aria-labelledby="reliability-narrative-title">
      <header>
        <p className="reliability-eyebrow">RecallGuard proof path</p>
        <h2 id="reliability-narrative-title">Detection is only the beginning.</h2>
        <p>Fixture values below come directly from the deterministic replay.</p>
      </header>
      <ol>
        <li className="is-healthy">
          <span>01</span>
          <small>Healthy baseline</small>
          <strong>{hazard ? `Hazard ${hazard.baseline_pct}%` : "Waiting for replay"}</strong>
        </li>
        <li className={demo?.incident_id ? "is-drift" : "is-pending"}>
          <span>02</span>
          <small>Drift detected</small>
          <strong>
            {hazard && driftValue !== undefined
              ? `${hazard.baseline_pct}% → ${driftValue}%`
              : "Pending"}
          </strong>
          {demo?.incident_id ? <em>Extraction drift detected</em> : null}
        </li>
        <li className={approvedRepair ? "is-validation" : "is-pending"}>
          <span>03</span>
          <small>Repair validation</small>
          <strong>
            {hazardValidation
              ? `${hazardValidation.before_pct}% → ${hazardValidation.after_pct}%`
              : "Pending"}
          </strong>
          {demo?.proof ? <em>Schema · semantics · source fidelity</em> : null}
        </li>
        <li className={demo?.proof ? "is-decision" : "is-pending"}>
          <span>04</span>
          <small>Decision</small>
          <strong>{demo?.proof?.decision ?? "Pending"}</strong>
          {demo?.proof ? <em>Fresh verification passed</em> : null}
        </li>
      </ol>
    </section>
  );
}

export function ReliabilityPage() {
  const [demo, setDemo] = useState<ReliabilityDemo | null>(null);
  const [liveEvidence, setLiveEvidence] = useState<LiveBrightDataEvidence | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback((signal?: AbortSignal) => {
    setError(null);
    return Promise.all([
      getReliabilityDemo(signal),
      getLiveBrightDataEvidence(signal),
    ])
      .then(([nextDemo, evidence]) => {
        setDemo(nextDemo);
        setLiveEvidence(evidence);
      })
      .catch((cause) => {
        if (isAbort(cause)) return;
        setError(describeError(cause));
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!running || !demo || demo.terminal) {
      if (demo?.terminal) setRunning(false);
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      advanceReliabilityDemo(controller.signal)
        .then((next) => {
          setDemo(next);
          if (next.terminal) setRunning(false);
        })
        .catch((cause) => {
          if (isAbort(cause)) return;
          setRunning(false);
          setError(describeError(cause));
        });
    }, STEP_DELAY_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [demo, running]);

  const runDemo = useCallback(async () => {
    setError(null);
    try {
      const started = await startReliabilityDemo();
      setDemo(started);
      setRunning(!started.terminal);
    } catch (cause) {
      setRunning(false);
      setError(describeError(cause));
    }
  }, []);

  const status = demo?.status ?? "healthy";
  const isUnstarted = demo?.session_id == null;

  return (
    <PageShell>
      <div className="reliability-page">
        <div className="container">
          <header className="reliability-hero">
            <div>
              <p className="reliability-eyebrow">RecallGuard reliability</p>
              <h1>Watch a scraper prove its own repair.</h1>
              <p className="reliability-deck">
                A deterministic replay of real extraction drift, repair gating,
                independent verification, and retained healing proof.
              </p>
            </div>
            <div className="reliability-control-card">
              <span className="reliability-mode">Fixture replay</span>
              <button
                className="reliability-run-button"
                type="button"
                onClick={runDemo}
                disabled={running}
              >
                {running
                  ? "Demo running…"
                  : isUnstarted
                    ? "Run self-healing demo"
                    : demo?.terminal
                      ? "Run demo again"
                      : "Resume self-healing demo"}
              </button>
              <p>Starts only when clicked. No production collector is targeted.</p>
            </div>
          </header>

          {error && (
            <div className="reliability-error" role="alert">
              <strong>Demo paused.</strong> {error}
            </div>
          )}

          <LiveEvidenceSection evidence={liveEvidence} />

          <div className="fixture-replay-divider">
            <span>Fixture replay</span>
            <div>
              <h2>Deterministic lifecycle demo</h2>
              <p>Safe, repeatable RecallGuard state transitions. No Bright Data job is implied.</p>
            </div>
          </div>

          <ReliabilityNarrative demo={demo} />

          <section className="reliability-health-card">
            <div>
              <span className="reliability-kicker">Collector</span>
              <h2>{demo?.collector_name ?? "RecallGuard self-healing demo"}</h2>
              <code>
                {demo?.external_collector_id ??
                  "gapradar-recallguard-self-healing-v1"}
              </code>
            </div>
            <span className={`reliability-status reliability-status--${status}`}>
              <span aria-hidden="true" />
              {STATUS_LABELS[status]}
            </span>
          </section>

          <div className="reliability-grid reliability-grid--summary">
            <section className="reliability-card reliability-incident-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Incident</span>
                  <h2>Classification</h2>
                </div>
                {demo?.severity && (
                  <span className="reliability-severity">{label(demo.severity)}</span>
                )}
              </div>
              {demo?.incident_id ? (
                <dl className="reliability-facts">
                  <div>
                    <dt>Classification</dt>
                    <dd>{label(demo.classification)}</dd>
                  </div>
                  <div>
                    <dt>Confidence</dt>
                    <dd>
                      {demo.confidence == null
                        ? "—"
                        : `${(demo.confidence * 100).toFixed(1)}%`}
                    </dd>
                  </div>
                  <div>
                    <dt>Affected fields</dt>
                    <dd>{demo.affected_fields.join(", ") || "—"}</dd>
                  </div>
                  <div>
                    <dt>Recommended action</dt>
                    <dd>{label(demo.recommended_action)}</dd>
                  </div>
                </dl>
              ) : (
                <p className="reliability-placeholder">
                  No active incident. The collector is at its verified baseline.
                </p>
              )}
            </section>

            <section className="reliability-card reliability-timeline-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Live sequence</span>
                  <h2>Incident timeline</h2>
                </div>
              </div>
              {demo?.timeline.length ? (
                <ol className="reliability-timeline">
                  {demo.timeline.map((event, index) => (
                    <li key={`${event.event}-${event.at}-${index}`}>
                      <span className="reliability-timeline-dot" aria-hidden="true" />
                      <div>
                        <strong>{EVENT_LABELS[event.event] ?? label(event.event)}</strong>
                        {timelineDetail(event) && <p>{timelineDetail(event)}</p>}
                      </div>
                      <time dateTime={event.at}>{formatTime(event.at)}</time>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="reliability-placeholder">
                  Run the demo to create an inspectable incident timeline.
                </p>
              )}
            </section>
          </div>

          <section className="reliability-card reliability-table-card">
            <div className="reliability-card-heading">
              <div>
                <span className="reliability-kicker">Coverage monitor</span>
                <h2>Extraction health</h2>
              </div>
              <span className="reliability-live-label">Backend values</span>
            </div>
            <div className="reliability-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Baseline</th>
                    <th>Current</th>
                    <th>Drop</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {(demo?.field_health ?? []).map((field) => (
                    <tr key={field.field}>
                      <td>{field.field}</td>
                      <td>{field.baseline_pct}%</td>
                      <td>{field.current_pct}%</td>
                      <td>{field.drop_pct == null ? "—" : `${field.drop_pct}%`}</td>
                      <td>
                        <span className={`field-status field-status--${field.status}`}>
                          {label(field.status)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <div className="reliability-grid reliability-grid--proof">
            <section className="reliability-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Repair panel</span>
                  <h2>Attempts &amp; regression guard</h2>
                </div>
              </div>
              {demo?.repair_attempts.length ? (
                <div className="repair-list">
                  {demo.repair_attempts.map((repair) => (
                    <article className={`repair-attempt repair-attempt--${repair.status}`} key={repair.attempt}>
                      <header>
                        <div>
                          <span>Attempt {repair.attempt}</span>
                          <h3>{repair.label}</h3>
                        </div>
                        <strong>{label(repair.status)}</strong>
                      </header>
                      <ul className="repair-changes">
                        {repair.changes.map((change) => (
                          <li key={change}>{change}</li>
                        ))}
                      </ul>
                      {repair.verification.length > 0 && (
                        <div className="repair-checks">
                          {repair.verification.map((check) => (
                            <div key={check.field}>
                              <span>{check.field}</span>
                              <code>
                                {check.before_pct} → {check.after_pct}
                              </code>
                              <strong className={`repair-check--${check.status}`}>
                                {check.status.toUpperCase()}
                              </strong>
                            </div>
                          ))}
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              ) : (
                <p className="reliability-placeholder">
                  Repair proposals will appear after drift is classified.
                </p>
              )}
            </section>

            <section className="reliability-card healing-proof-card">
              <div className="reliability-card-heading">
                <div>
                  <span className="reliability-kicker">Retained evidence</span>
                  <h2>Healing proof</h2>
                </div>
              </div>
              {demo?.proof ? (
                <>
                  <div className="fidelity-list">
                    <div>
                      <span>Schema fidelity</span>
                      <strong>{demo.proof.schema_fidelity}</strong>
                    </div>
                    <div>
                      <span>Semantic fidelity</span>
                      <strong>{demo.proof.semantic_fidelity}</strong>
                    </div>
                    <div>
                      <span>Source fidelity</span>
                      <strong>{demo.proof.source_fidelity}</strong>
                    </div>
                  </div>
                  <div className="proof-decision">
                    <span>Final decision</span>
                    <strong>{demo.proof.decision}</strong>
                  </div>
                </>
              ) : (
                <div className="proof-pending">
                  <span aria-hidden="true">◎</span>
                  <p>
                    Proof remains pending until a fresh run passes the contract
                    and the regression guard.
                  </p>
                </div>
              )}
            </section>
          </div>
        </div>
      </div>
    </PageShell>
  );
}
