import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, NetworkError } from "../api/client";
import { getOpportunity, getOpportunityResearch } from "../api/opportunities";
import { toProblem } from "../api/adapters";
import type { ResearchIntelligence } from "../api/types";
import type { Problem } from "../types";
import { ResearchSection } from "./ResearchSection";
import { formatRelativeDate } from "../utils/formatDate";
import "./ProblemDetail.css";

/** The four 1-10 component scores. `itch` is 0-100 and is shown separately. */
const SCORE_LABELS: { key: keyof Problem["scores"]; label: string }[] = [
  { key: "severity", label: "Severity" },
  { key: "tam", label: "Market size" },
  { key: "whitespace", label: "Whitespace" },
  { key: "frequency", label: "Frequency" },
];

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "Could not reach the API. Check the backend is running and that this origin is allowed.";
  }

  if (error instanceof ApiError) {
    return error.isNotFound
      ? "This opportunity is not available."
      : error.message;
  }

  return "Something went wrong.";
}

export function ProblemDetail({
  problem,
  onClose,
}: {
  problem: Problem;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  // Rendered optimistically from the feed row -- the detail endpoint returns
  // the identical shape -- then replaced by the authoritative record.
  const [detail, setDetail] = useState<Problem>(problem);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [research, setResearch] = useState<ResearchIntelligence | null>(null);
  const [researchLoading, setResearchLoading] = useState(true);
  const [researchError, setResearchError] = useState<string | null>(null);
  const [researchAttempt, setResearchAttempt] = useState(0);

  useEffect(() => {
    closeButtonRef.current?.focus();

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
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

  return (
    <div className="problem-detail-overlay" onClick={onClose}>
      <div
        className="problem-detail"
        role="dialog"
        aria-modal="true"
        aria-labelledby="problem-detail-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="problem-detail-header">
          {detail.category && (
            <span className="problem-detail-tag">{detail.category}</span>
          )}

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

        <h2 id="problem-detail-title" className="problem-detail-title">
          {detail.title}
        </h2>

        {detailError && (
          <p className="problem-detail-notice">{detailError}</p>
        )}

        <div className="problem-detail-meta">
          {detail.source && (
            <>
              <span>{detail.source}</span>
              <span aria-hidden="true">·</span>
            </>
          )}

          <span>{formatRelativeDate(detail.date)}</span>

          <span aria-hidden="true">·</span>

          {/* Not scorable is a backend answer, never a zero. */}
          <span>
            {detail.signal === null
              ? "Not scorable"
              : `Opportunity score ${detail.signal}`}
          </span>
        </div>

        <p className="problem-detail-description">
          {detail.description}
        </p>

        <dl className="problem-detail-scores">
          {detail.scores.itch !== null && (
            <div className="problem-detail-score">
              <dt>Itch</dt>

              <dd>
                <div className="problem-detail-score-bar">
                  <div
                    className="problem-detail-score-fill"
                    style={{
                      width: `${detail.scores.itch}%`,
                    }}
                  />
                </div>

                <span>{detail.scores.itch}/100</span>
              </dd>
            </div>
          )}

          {SCORE_LABELS.map(({ key, label }) => {
            const value = detail.scores[key];

            if (value === null) {
              return null;
            }

            return (
              <div className="problem-detail-score" key={key}>
                <dt>{label}</dt>

                <dd>
                  <div className="problem-detail-score-bar">
                    <div
                      className="problem-detail-score-fill"
                      style={{
                        width: `${(value / 10) * 100}%`,
                      }}
                    />
                  </div>

                  <span>{value}/10</span>
                </dd>
              </div>
            );
          })}
        </dl>

        {detail.sourceUrl && (
          <a
            className="problem-detail-link"
            href={detail.sourceUrl}
            target="_blank"
            rel="noreferrer"
          >
            View source ↗
          </a>
        )}

        <ResearchSection
          research={research}
          loading={researchLoading}
          error={researchError}
          onRetry={retryResearch}
        />
      </div>
    </div>
  );
}