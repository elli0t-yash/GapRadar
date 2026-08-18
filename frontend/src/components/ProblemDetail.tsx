import { useEffect, useRef } from "react";
import type { Problem } from "../types";
import { formatRelativeDate } from "../utils/formatDate";
import "./ProblemDetail.css";

const SCORE_LABELS: { key: keyof Problem["scores"]; label: string }[] = [
  { key: "severity", label: "Severity" },
  { key: "tam", label: "Market size" },
  { key: "whitespace", label: "Whitespace" },
  { key: "frequency", label: "Frequency" },
];

export function ProblemDetail({
  problem,
  onClose,
}: {
  problem: Problem;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeButtonRef.current?.focus();

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

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
          <span className="problem-detail-tag">{problem.category}</span>
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
          {problem.title}
        </h2>

        <div className="problem-detail-meta">
          <span>{problem.source}</span>
          <span aria-hidden="true">·</span>
          <span>{formatRelativeDate(problem.date)}</span>
          <span aria-hidden="true">·</span>
          <span>Signal {problem.signal}</span>
        </div>

        <p className="problem-detail-description">{problem.description}</p>

        <dl className="problem-detail-scores">
          {SCORE_LABELS.map(({ key, label }) => (
            <div className="problem-detail-score" key={key}>
              <dt>{label}</dt>
              <dd>
                <div className="problem-detail-score-bar">
                  <div
                    className="problem-detail-score-fill"
                    style={{ width: `${(problem.scores[key] / 10) * 100}%` }}
                  />
                </div>
                <span>{problem.scores[key]}/10</span>
              </dd>
            </div>
          ))}
        </dl>

        {problem.sourceUrl && (
          <a
            className="problem-detail-link"
            href={problem.sourceUrl}
            target="_blank"
            rel="noreferrer"
          >
            View source ↗
          </a>
        )}
      </div>
    </div>
  );
}
