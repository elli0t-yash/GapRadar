import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, NetworkError } from "../api/client";
import {
  getOpportunity,
  getOpportunityResearch,
  getResearchEnrichment,
  startResearchEnrichment,
} from "../api/opportunities";
import { toProblem } from "../api/adapters";
import type {
  ResearchEnrichmentStatus,
  ResearchEnrichmentCounters,
  ResearchOutcomeReason,
  ResearchQueryState,
  ResearchIntelligence,
} from "../api/types";
import { isEnrichmentTerminal } from "../api/types";
import type { Problem } from "../types";
import { ResearchSection } from "./ResearchSection";
import { formatRelativeDate } from "../utils/formatDate";
import "./ProblemDetail.css";

/** The four 1-10 component scores. `itch` is 0-100 and is shown separately. */
const SCORE_LABELS: { key: keyof Problem["scores"]; label: string }[] = [
  { key: "severity", label: "Severity" },
  { key: "tam", label: "Market potential" },
  { key: "whitespace", label: "Whitespace" },
  { key: "frequency", label: "Frequency" },
];

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "We couldn't connect to GapRadar. The opportunity already on screen remains available.";
  }

  if (error instanceof ApiError) {
    return error.isNotFound
      ? "This opportunity is not available."
      : error.message;
  }

  return "We couldn't load this intelligence.";
}

// A run that has reported nothing yet, and the shape old runs
// (counters={}) deserialize to. Frozen so no consumer can mutate the
// shared default.
const EMPTY_COUNTERS: ResearchEnrichmentCounters = Object.freeze({
  discovered: 0,
  selected: 0,
  judged: 0,
  matched: 0,
});

export function ProblemDetail({
  problem,
  onClose,
}: {
  problem: Problem;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Rendered optimistically from the feed row -- the detail endpoint returns
  // the identical shape -- then replaced by the authoritative record.
  const [detail, setDetail] = useState<Problem>(problem);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [research, setResearch] = useState<ResearchIntelligence | null>(null);
  const [researchLoading, setResearchLoading] = useState(true);
  const [researchError, setResearchError] = useState<string | null>(null);
  const [researchAttempt, setResearchAttempt] = useState(0);

  useEffect(() => {
    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
      }
      if (e.key !== "Tab" || !dialogRef.current) return;

      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;

      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus();
    };
  }, [onClose]);

  // The card object is a cached row, not the authority.
  // Refetch to confirm.
  //
  // We deliberately use an active flag instead of aborting the request.
  // React StrictMode mounts / cleans up / mounts effects again in development.
  // Aborting during that cleanup causes Chrome DevTools to display a failed
  // request even though the following request succeeds.
  useEffect(() => {
    let active = true;

    setDetail(problem);
    setDetailError(null);

    getOpportunity(problem.id)
      .then((opportunity) => {
        if (!active) return;

        setDetail(toProblem(opportunity));
      })
      .catch((error) => {
        if (!active) return;

        setDetailError(describeError(error));
      });

    return () => {
      active = false;
    };
  }, [problem]);

  // Research is fetched separately so a failure here leaves the opportunity
  // detail fully usable.
  useEffect(() => {
    let active = true;

    setResearchLoading(true);
    setResearchError(null);

    getOpportunityResearch(problem.id)
      .then((intelligence) => {
        if (!active) return;

        setResearch(intelligence);
        setResearchLoading(false);
      })
      .catch((error) => {
        if (!active) return;

        setResearch(null);
        setResearchError(describeError(error));
        setResearchLoading(false);
      });

    return () => {
      active = false;
    };
  }, [problem.id, researchAttempt]);

  const retryResearch = useCallback(() => {
    setResearchAttempt((n) => n + 1);
  }, []);

  // --- on-demand research enrichment -----------------------------------
  // Reflected from the backend job. Nothing in this block starts work:
  // `analyse` runs only from the button, and the effects only read.
  const [enrichmentStatus, setEnrichmentStatus] =
    useState<ResearchEnrichmentStatus | null>(null);
  const [enrichmentError, setEnrichmentError] = useState<string | null>(null);
  const [enrichmentWarning, setEnrichmentWarning] = useState<string | null>(
    null,
  );
  // Backend-reported per-query progress. Never synthesised, never
  // advanced on a timer: an empty list means the backend has not said
  // anything yet, and the UI says exactly that.
  const [enrichmentQueryStates, setEnrichmentQueryStates] = useState<
    ResearchQueryState[]
  >([]);
  const [enrichmentCounters, setEnrichmentCounters] =
    useState<ResearchEnrichmentCounters>(EMPTY_COUNTERS);
  // Typed outcome + backend-computed retryability. Reflected verbatim:
  // this component never decides whether a retry makes sense.
  const [enrichmentOutcomeReason, setEnrichmentOutcomeReason] =
    useState<ResearchOutcomeReason | null>(null);
  const [enrichmentIsRetryable, setEnrichmentIsRetryable] = useState(false);
  // Guards a genuine double-click. StrictMode re-invokes effects, never
  // handlers, so this is the only race it has to close -- the backend's
  // active-job constraint stays the authority.
  const starting = useRef(false);
  // The same guard, as state, so the button can be disabled the instant
  // it is pressed. The ref alone cannot do that: mutating it does not
  // re-render, which is what left the CTA clickable during the POST.
  const [enrichmentStarting, setEnrichmentStarting] = useState(false);

  // Adopt a job already running for this opportunity so a reload mid-run
  // shows progress instead of the call to action. A pure read.
  useEffect(() => {
    let active = true;

    setEnrichmentStatus(null);
    setEnrichmentError(null);
    setEnrichmentWarning(null);
    setEnrichmentQueryStates([]);
    setEnrichmentCounters(EMPTY_COUNTERS);
    setEnrichmentOutcomeReason(null);
    setEnrichmentIsRetryable(false);

    getResearchEnrichment(problem.id)
      .then((job) => {
        if (!active || !job) return;

        setEnrichmentStatus(job.status);
        setEnrichmentQueryStates(job.query_states);
        setEnrichmentCounters(job.counters);
        setEnrichmentWarning(job.warning);
        setEnrichmentOutcomeReason(job.outcome_reason);
        setEnrichmentIsRetryable(job.is_retryable);
        if (job.status === "failed") setEnrichmentError(job.error);
      })
      .catch(() => {
        // A status read failing must never block the call to action.
      });

    return () => {
      active = false;
    };
  }, [problem.id]);

  // Poll only while a job is active, and stop as soon as it is terminal.
  useEffect(() => {
    if (enrichmentStatus !== "queued" && enrichmentStatus !== "running") return;

    let active = true;

    const timer = window.setInterval(() => {
      getResearchEnrichment(problem.id)
        .then((job) => {
          if (!active || !job) return;

          setEnrichmentStatus(job.status);
          setEnrichmentQueryStates(job.query_states);
          setEnrichmentCounters(job.counters);
          setEnrichmentWarning(job.warning);
          setEnrichmentOutcomeReason(job.outcome_reason);
          setEnrichmentIsRetryable(job.is_retryable);
          if (job.status === "failed") setEnrichmentError(job.error);
          if (isEnrichmentTerminal(job.status)) {
            window.clearInterval(timer);
            // The job is finished; the research read model is now the
            // truth, so refetch it rather than trusting the job record.
            if (job.status === "succeeded") setResearchAttempt((n) => n + 1);
          }
        })
        .catch(() => {
          // A dropped poll is retryable: the job is safe server-side.
        });
    }, 4000);

    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [problem.id, enrichmentStatus]);

  // THE ONLY CALL THAT SPENDS A PROVIDER RUN. Invoked from the button and
  // from nowhere else -- never from an effect, never on mount.
  const analyse = useCallback(() => {
    if (starting.current) return;
    starting.current = true;
    setEnrichmentStarting(true);

    setEnrichmentError(null);
    setEnrichmentWarning(null);
    setEnrichmentQueryStates([]);
    setEnrichmentCounters(EMPTY_COUNTERS);
    setEnrichmentOutcomeReason(null);
    setEnrichmentIsRetryable(false);
    setEnrichmentStatus("queued");

    startResearchEnrichment(problem.id)
      .then((accepted) => {
        // already_enriched and already_running are both SUCCESS answers.
        if (accepted.already_enriched) {
          setEnrichmentStatus("succeeded");
          setResearchAttempt((n) => n + 1);
          return;
        }
        setEnrichmentStatus(accepted.status);
      })
      .catch((error) => {
        setEnrichmentStatus("failed");
        setEnrichmentError(describeError(error));
      })
      .finally(() => {
        starting.current = false;
        setEnrichmentStarting(false);
      });
  }, [problem.id]);

  const hasScoreBreakdown =
    detail.scores.itch !== null ||
    SCORE_LABELS.some(({ key }) => detail.scores[key] !== null);

  return (
    <div className="problem-detail-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        className="problem-detail"
        role="dialog"
        aria-modal="true"
        aria-labelledby="problem-detail-title"
        aria-describedby="problem-detail-description"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="problem-detail-header">
          <button type="button" className="problem-detail-back" onClick={onClose}>
            <span aria-hidden="true">←</span> Opportunities
          </button>

          <button
            ref={closeButtonRef}
            type="button"
            className="problem-detail-close"
            onClick={onClose}
            aria-label="Close details"
          >
            ×
          </button>
        </div>

        <header className="problem-detail-report-header">
          <div>
            <div className="problem-detail-kickers">
              {detail.category && (
                <span className="problem-detail-tag">{detail.category}</span>
              )}
              <span
                className="problem-detail-trust"
                title="This opportunity is visible because its source collector currently passes GapRadar reliability gating."
              >
                <span aria-hidden="true">✓</span> Trusted source
              </span>
            </div>
            <p className="problem-detail-eyebrow">Opportunity intelligence report</p>
            <h2 id="problem-detail-title" className="problem-detail-title">
              {detail.title}
            </h2>
          </div>

          <div className="problem-detail-score-hero">
            <span>Opportunity score</span>
            <strong>{detail.signal === null ? "—" : detail.signal}</strong>
            <small>{detail.signal === null ? "Not scorable" : "out of 100"}</small>
          </div>
        </header>

        {detailError && (
          <p className="problem-detail-notice">{detailError}</p>
        )}

        <div className="problem-detail-meta" aria-label="Opportunity provenance">
          {detail.source && (
            <>
              <span>{detail.source}</span>
              <span aria-hidden="true">·</span>
            </>
          )}

          <span>{formatRelativeDate(detail.date)}</span>

        </div>

        <section className="problem-detail-section" aria-labelledby="problem-overview-title">
          <p className="problem-detail-section-label">Meaning</p>
          <h3 id="problem-overview-title">Overview</h3>
          <p id="problem-detail-description" className="problem-detail-description">
            {detail.description}
          </p>
        </section>

        <section className="problem-detail-section" aria-labelledby="problem-evidence-title">
          <p className="problem-detail-section-label">Source trail</p>
          <h3 id="problem-evidence-title">Evidence</h3>
          <div className="problem-detail-evidence-row">
            <div>
              <strong>{detail.source ?? "Persisted source"}</strong>
              <span>Observed {formatRelativeDate(detail.date)}</span>
            </div>
            {detail.sourceUrl && (
              <a
                className="problem-detail-link"
                href={detail.sourceUrl}
                target="_blank"
                rel="noreferrer"
              >
                View original source <span aria-hidden="true">↗</span>
              </a>
            )}
          </div>
        </section>

        {hasScoreBreakdown ? (
          <details className="problem-detail-breakdown">
            <summary>Inspect score breakdown</summary>
            <p>
              Component scores are backend assessments, shown on their native
              scales without converting them into percentages.
            </p>
            <dl className="problem-detail-scores">
              {detail.scores.itch !== null && (
                <div className="problem-detail-score">
                  <dt>Problem intensity</dt>
                  <dd>{detail.scores.itch}/100</dd>
                </div>
              )}
              {SCORE_LABELS.map(({ key, label }) => {
                const value = detail.scores[key];
                if (value === null) return null;
                return (
                  <div className="problem-detail-score" key={key}>
                    <dt>{label}</dt>
                    <dd>{value}/10</dd>
                  </div>
                );
              })}
            </dl>
          </details>
        ) : null}

        <section
          className="problem-detail-section is-reliability"
          aria-labelledby="problem-reliability-title"
        >
          <p className="problem-detail-section-label">Source health</p>
          <h3 id="problem-reliability-title">Reliability</h3>
          <div className="problem-detail-reliability-row">
            <span aria-hidden="true">✓</span>
            <div>
              <strong>Trusted data</strong>
              <p>
                This opportunity is visible because its source collector passes
                GapRadar reliability gating.
              </p>
            </div>
            <Link to="/reliability">Inspect RecallGuard <span aria-hidden="true">→</span></Link>
          </div>
        </section>

        <ResearchSection
          research={research}
          loading={researchLoading}
          error={researchError}
          onRetry={retryResearch}
          enrichmentStatus={enrichmentStatus}
          enrichmentError={enrichmentError}
          enrichmentQueryStates={enrichmentQueryStates}
          enrichmentCounters={enrichmentCounters}
          enrichmentOutcomeReason={enrichmentOutcomeReason}
          enrichmentIsRetryable={enrichmentIsRetryable}
          enrichmentStarting={enrichmentStarting}
          enrichmentWarning={enrichmentWarning}
          onAnalyse={analyse}
        />
      </div>
    </div>
  );
}
