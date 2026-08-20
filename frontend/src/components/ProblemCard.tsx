import type { Problem } from "../types";
import { formatRelativeDate } from "../utils/formatDate";
import "./ProblemCard.css";

export function ProblemCard({
  problem,
  onOpen,
  rank,
  featured = false,
}: {
  problem: Problem;
  onOpen: (problem: Problem) => void;
  rank?: number;
  featured?: boolean;
}) {
  return (
    <article
      className={`problem-card${featured ? " is-featured" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(problem)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(problem);
        }
      }}
      aria-label={`View details for ${problem.title}`}
    >
      {rank ? (
        <div className="problem-card-rank" aria-label={`Rank ${rank}`}>
          <span>{String(rank).padStart(2, "0")}</span>
          {featured ? <strong>Highest ranked</strong> : null}
        </div>
      ) : null}
      <div className="problem-card-top">
        {problem.category && (
          <span className="problem-card-tag">{problem.category}</span>
        )}
        {problem.signal === null ? (
          // Not scorable is a real backend answer and must never render as 0.
          <span className="problem-card-signal is-unscored" title="Not scorable">
            Not scorable
          </span>
        ) : (
          <span
            className={`problem-card-signal${problem.signal >= 85 ? " is-hot" : ""}`}
            title="Opportunity score returned by GapRadar"
          >
            <small>Opportunity score</small>
            <strong>{problem.signal}</strong>
          </span>
        )}
      </div>

      <h3 className="problem-card-title">{problem.title}</h3>
      <p className="problem-card-description">{problem.description}</p>

      <div className="problem-card-meta">
        <span
          className="problem-card-trust"
          title="This opportunity is visible because its source collector currently passes GapRadar reliability gating."
        >
          <span aria-hidden="true">✓</span> Trusted source
        </span>
        {problem.source && (
          <>
            <span className="problem-card-source">{problem.source}</span>
            <span className="problem-card-dot" aria-hidden="true">
              ·
            </span>
          </>
        )}
        <span className="problem-card-date">
          {formatRelativeDate(problem.date)}
        </span>
        <svg
          className="problem-card-arrow"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M3.5 8h9M8.5 3.5 13 8l-4.5 4.5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <span className="problem-card-cta" aria-hidden="true">
        Explore opportunity <span>→</span>
      </span>
    </article>
  );
}
