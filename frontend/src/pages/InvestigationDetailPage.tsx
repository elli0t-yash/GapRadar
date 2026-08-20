import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { isAbort, NetworkError } from "../api/client";
import {
  getInvestigation,
  getInvestigationCompetitors,
  getInvestigationEvidence,
  getInvestigationResearch,
  getInvestigationRun,
  startInvestigationRun,
} from "../api/investigations";
import type {
  CompetitorCollection,
  DemandEvidenceCollection,
  Investigation,
  InvestigationResearchIntelligence,
  InvestigationRun,
} from "../api/investigationTypes";
import { ErrorState } from "../components/ErrorState";
import { CompetitorSection } from "../components/CompetitorSection";
import { DemandEvidenceSection } from "../components/DemandEvidenceSection";
import { InvestigationProgress } from "../components/InvestigationProgress";
import { InvestigationResearchSection } from "../components/InvestigationResearchSection";
import { PageShell } from "../components/PageShell";
import "./InvestigationDetailPage.css";

const POLL_DELAY_MS = 1_500;
const POLL_RETRY_DELAY_MS = 4_000;

type SurfaceStatus = "idle" | "loading" | "loaded" | "failed";

interface TerminalResults {
  research: InvestigationResearchIntelligence | null;
  evidence: DemandEvidenceCollection | null;
  competitors: CompetitorCollection | null;
}

interface SurfaceLoadState {
  status: SurfaceStatus;
  error: string | null;
}

interface TerminalSurfaceState {
  research: SurfaceLoadState;
  evidence: SurfaceLoadState;
  competitors: SurfaceLoadState;
}

const EMPTY_RESULTS: TerminalResults = {
  research: null,
  evidence: null,
  competitors: null,
};

const IDLE_SURFACES: TerminalSurfaceState = {
  research: { status: "idle", error: null },
  evidence: { status: "idle", error: null },
  competitors: { status: "idle", error: null },
};

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "We couldn't connect to GapRadar. Your persisted investigation is safe.";
  }
  return error instanceof Error ? error.message : "We couldn't load this intelligence.";
}

function displayLabel(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function settledSurface(
  result: PromiseSettledResult<unknown>,
): SurfaceLoadState {
  if (result.status === "fulfilled") {
    return { status: "loaded", error: null };
  }
  return {
    status: "failed",
    error: isAbort(result.reason) ? null : describeError(result.reason),
  };
}

function surfaceStatusLabel(status: SurfaceStatus): string {
  if (status === "loaded") return "Loaded";
  if (status === "failed") return "Failed to load";
  return displayLabel(status);
}

function ResultSurfaceState({
  title,
  surface,
}: {
  title: string;
  surface: SurfaceLoadState;
}) {
  const failed = surface.status === "failed";
  return (
    <section
      className={`investigation-result-state${failed ? " is-error" : ""}`}
      aria-live="polite"
    >
      <p className="investigation-result-state-label">Persisted results</p>
      <h2>{title}</h2>
      <p role={failed ? "alert" : undefined}>
        {failed
          ? (surface.error ?? "This result surface could not be loaded.")
          : "Loading persisted results…"}
      </p>
    </section>
  );
}

export function InvestigationDetailPage() {
  const { investigationId: id } = useParams<{ investigationId: string }>();
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [run, setRun] = useState<InvestigationRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [awaitingRun, setAwaitingRun] = useState(false);
  const [terminalResults, setTerminalResults] =
    useState<TerminalResults>(EMPTY_RESULTS);
  const [terminalSurfaces, setTerminalSurfaces] =
    useState<TerminalSurfaceState>(IDLE_SURFACES);

  const initialLoadGenerationRef = useRef(0);
  const pollGenerationRef = useRef(0);
  const pollTimerRef = useRef<number | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const terminalGenerationRef = useRef(0);
  const terminalAbortRef = useRef<AbortController | null>(null);
  const terminalRunIdRef = useRef<string | null>(null);
  const runPostInFlightRef = useRef(false);

  const stopPolling = useCallback(() => {
    pollGenerationRef.current += 1;
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
  }, []);

  const resetTerminalResults = useCallback(() => {
    terminalGenerationRef.current += 1;
    terminalAbortRef.current?.abort();
    terminalAbortRef.current = null;
    terminalRunIdRef.current = null;
    setTerminalResults(EMPTY_RESULTS);
    setTerminalSurfaces(IDLE_SURFACES);
  }, []);

  const loadTerminalResults = useCallback(
    async (terminalRun: InvestigationRun) => {
      if (!id || terminalRunIdRef.current === terminalRun.run_id) return;

      terminalAbortRef.current?.abort();
      const controller = new AbortController();
      terminalAbortRef.current = controller;
      const generation = ++terminalGenerationRef.current;
      terminalRunIdRef.current = terminalRun.run_id;
      setTerminalSurfaces({
        research: { status: "loading", error: null },
        evidence: { status: "loading", error: null },
        competitors: { status: "loading", error: null },
      });

      const [research, evidence, competitors] = await Promise.allSettled([
        getInvestigationResearch(id, controller.signal),
        getInvestigationEvidence(id, controller.signal),
        getInvestigationCompetitors(id, controller.signal),
      ]);

      if (generation !== terminalGenerationRef.current) return;

      setTerminalResults({
        research: research.status === "fulfilled" ? research.value : null,
        evidence: evidence.status === "fulfilled" ? evidence.value : null,
        competitors: competitors.status === "fulfilled" ? competitors.value : null,
      });
      setTerminalSurfaces({
        research: settledSurface(research),
        evidence: settledSurface(evidence),
        competitors: settledSurface(competitors),
      });
    },
    [id],
  );

  const attachPolling = useCallback(
    (delayMs = 0, expectedRunId?: string) => {
      if (!id) return;

      stopPolling();
      const generation = pollGenerationRef.current;

      const poll = async () => {
        if (generation !== pollGenerationRef.current) return;

        const controller = new AbortController();
        pollAbortRef.current = controller;

        try {
          const latestRun = await getInvestigationRun(id, controller.signal);
          if (generation !== pollGenerationRef.current) return;

          if (expectedRunId && latestRun && latestRun.run_id !== expectedRunId) {
            pollTimerRef.current = window.setTimeout(
              () => void poll(),
              POLL_DELAY_MS,
            );
            return;
          }

          setPollError(null);
          setAwaitingRun(false);
          setRun(latestRun);

          if (latestRun === null) return;
          if (latestRun.is_terminal) {
            void loadTerminalResults(latestRun);
            return;
          }

          pollTimerRef.current = window.setTimeout(
            () => void poll(),
            POLL_DELAY_MS,
          );
        } catch (error) {
          if (generation !== pollGenerationRef.current || isAbort(error)) return;
          setPollError(describeError(error));
          pollTimerRef.current = window.setTimeout(
            () => void poll(),
            POLL_RETRY_DELAY_MS,
          );
        } finally {
          if (pollAbortRef.current === controller) pollAbortRef.current = null;
        }
      };

      if (delayMs > 0) {
        pollTimerRef.current = window.setTimeout(() => void poll(), delayMs);
      } else {
        void poll();
      }
    },
    [id, loadTerminalResults, stopPolling],
  );

  const loadPage = useCallback(
    async (signal?: AbortSignal) => {
      if (!id) {
        setLoading(false);
        setPageError("This investigation URL is missing its id.");
        return;
      }

      const generation = ++initialLoadGenerationRef.current;
      setLoading(true);
      setPageError(null);

      try {
        const [loadedInvestigation, latestRun] = await Promise.all([
          getInvestigation(id, signal),
          getInvestigationRun(id, signal),
        ]);
        if (generation !== initialLoadGenerationRef.current) return;

        setInvestigation(loadedInvestigation);
        setRun(latestRun);
        setAwaitingRun(false);

        if (latestRun?.is_terminal) {
          void loadTerminalResults(latestRun);
        } else if (latestRun) {
          attachPolling(POLL_DELAY_MS);
        }
      } catch (error) {
        if (generation !== initialLoadGenerationRef.current || isAbort(error)) return;
        setPageError(describeError(error));
      } finally {
        if (generation === initialLoadGenerationRef.current) setLoading(false);
      }
    },
    [attachPolling, id, loadTerminalResults],
  );

  useEffect(() => {
    const controller = new AbortController();
    setInvestigation(null);
    setRun(null);
    setPollError(null);
    setStartError(null);
    setAwaitingRun(false);
    resetTerminalResults();
    void loadPage(controller.signal);

    return () => {
      controller.abort();
      initialLoadGenerationRef.current += 1;
      stopPolling();
      terminalGenerationRef.current += 1;
      terminalAbortRef.current?.abort();
      terminalAbortRef.current = null;
    };
  }, [loadPage, resetTerminalResults, stopPolling]);

  const handleRunRequest = useCallback(async () => {
    if (!id || runPostInFlightRef.current) return;

    runPostInFlightRef.current = true;
    setStarting(true);
    setStartError(null);
    setPollError(null);

    try {
      const accepted = await startInvestigationRun(id);
      resetTerminalResults();
      setAwaitingRun(true);
      attachPolling(0, accepted.run_id);
    } catch (error) {
      if (!isAbort(error)) setStartError(describeError(error));
    } finally {
      runPostInFlightRef.current = false;
      setStarting(false);
    }
  }, [attachPolling, id, resetTerminalResults]);

  const resultSurfaces = [
    {
      label: "Research",
      status: terminalSurfaces.research.status,
    },
    {
      label: "Demand evidence",
      status: terminalSurfaces.evidence.status,
    },
    {
      label: "Competitor candidates",
      status: terminalSurfaces.competitors.status,
    },
  ];

  if (loading) {
    return (
      <PageShell>
        <div className="container investigation-detail-page">
          <div className="investigation-loading" role="status" aria-label="Loading investigation">
            <span className="investigation-loading-line is-label" />
            <span className="investigation-loading-line is-title" />
            <span className="investigation-loading-line is-title-short" />
            <div className="investigation-loading-metrics">
              <span /><span /><span />
            </div>
          </div>
        </div>
      </PageShell>
    );
  }

  if (pageError || !investigation) {
    return (
      <PageShell>
        <div className="container investigation-detail-page">
          <ErrorState
            title="We couldn't load this intelligence"
            message={pageError ?? "The investigation could not be found."}
            onRetry={() => void loadPage()}
            note="Your persisted data is safe."
          />
        </div>
      </PageShell>
    );
  }

  const actionBusy = starting || awaitingRun;

  return (
    <PageShell>
      <div className="container investigation-detail-page">
        <header className="investigation-detail-header">
          <Link className="investigation-back" to="/investigate">
            <span aria-hidden="true">←</span> All investigations
          </Link>
          <p className="investigation-eyebrow">User-supplied hypothesis</p>
          <h1>{investigation.query}</h1>
          {investigation.description ? (
            <p className="investigation-query">{investigation.description}</p>
          ) : null}
          <div className="investigation-meta">
            {investigation.industry ? <span>{investigation.industry}</span> : null}
            <span>{displayLabel(investigation.status)}</span>
          </div>
        </header>

        {run === null && !awaitingRun ? (
          <section className="investigation-analyse-card">
            <div>
              <h2>Ready to investigate</h2>
              <p>
                Analysis starts only when you ask. Opening or reloading this page
                never starts a provider run.
              </p>
            </div>
            <button
              type="button"
              className="investigation-primary-action"
              disabled={actionBusy}
              onClick={() => void handleRunRequest()}
            >
              {starting ? "Starting…" : "Analyse Idea"}
            </button>
          </section>
        ) : null}

        {awaitingRun && run === null ? (
          <section className="investigation-run-card" role="status">
            <p className="investigation-eyebrow">Analysis requested</p>
            <h2>Connecting to the persisted run…</h2>
          </section>
        ) : null}

        {run ? (
          <section className="investigation-run-card" aria-labelledby="run-progress-title">
            <div className="investigation-run-header">
              <div>
                <p className="investigation-eyebrow">Investigation journey</p>
                <h2 id="run-progress-title">Hypothesis → evidence</h2>
              </div>
              <span
                className={`investigation-run-status${run.is_terminal ? " is-terminal" : ""}`}
              >
                {displayLabel(run.status)}
              </span>
            </div>

            {run.warning ? (
              <p className="investigation-notice is-warning">{run.warning}</p>
            ) : null}
            {run.error ? (
              <p className="investigation-notice is-error">{run.error}</p>
            ) : null}
            {pollError ? (
              <p className="investigation-notice is-warning">
                Status refresh failed; polling will retry. {pollError}
              </p>
            ) : null}

            <dl className="investigation-summary" aria-label="Measured investigation summary">
              <div>
                <dt>Academic research</dt>
                <dd>{run.phases.research.matched}</dd>
                <span>relevant papers</span>
              </div>
              <div>
                <dt>Demand evidence</dt>
                <dd>{run.phases.demand.accepted}</dd>
                <span>accepted</span>
              </div>
              <div>
                <dt>Competitor candidates</dt>
                <dd>{run.phases.competitors.accepted}</dd>
                <span>accepted</span>
              </div>
            </dl>

            <InvestigationProgress run={run} />

            {run.is_retryable ? (
              <button
                type="button"
                className="investigation-primary-action investigation-retry"
                disabled={actionBusy}
                onClick={() => void handleRunRequest()}
              >
                {starting || awaitingRun ? "Retrying…" : "Retry"}
              </button>
            ) : null}
          </section>
        ) : null}

        {run?.is_terminal ? (
          <section className="investigation-results-status" aria-labelledby="result-load-title">
            <div>
              <p className="investigation-eyebrow">Evidence workspace</p>
              <h2 id="result-load-title">Inspect the persisted results</h2>
              <p>Move from research to market evidence, then test the competitive gap.</p>
            </div>
            <div className="investigation-surface-list">
              {resultSurfaces.map((surface, index) => (
                <a
                  key={surface.label}
                  href={[
                    "#investigation-research-title",
                    "#demand-evidence-title",
                    "#competitor-candidates-title",
                  ][index]}
                >
                  <span>{surface.label}</span>
                  <strong className={`is-${surface.status}`}>
                    {surfaceStatusLabel(surface.status)}
                  </strong>
                </a>
              ))}
            </div>
          </section>
        ) : null}

        {run?.is_terminal ? (
          <div className="investigation-result-surfaces">
            {terminalSurfaces.research.status === "loaded" &&
            terminalResults.research ? (
              <InvestigationResearchSection
                research={terminalResults.research}
                partial={run.phases.research.state === "partial"}
              />
            ) : (
              <ResultSurfaceState
                title="Relevant academic research"
                surface={terminalSurfaces.research}
              />
            )}

            {terminalSurfaces.evidence.status === "loaded" &&
            terminalResults.evidence ? (
              <DemandEvidenceSection
                collection={terminalResults.evidence}
                partial={run.phases.demand.state === "partial"}
              />
            ) : (
              <ResultSurfaceState
                title="Demand evidence"
                surface={terminalSurfaces.evidence}
              />
            )}

            {terminalSurfaces.competitors.status === "loaded" &&
            terminalResults.competitors ? (
              <CompetitorSection
                collection={terminalResults.competitors}
                partial={run.phases.competitors.state === "partial"}
              />
            ) : (
              <ResultSurfaceState
                title="Competitor candidates"
                surface={terminalSurfaces.competitors}
              />
            )}
          </div>
        ) : null}

        {startError ? (
          <p className="investigation-notice is-error" role="alert">
            {startError}
          </p>
        ) : null}
      </div>
    </PageShell>
  );
}
