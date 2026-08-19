import type { Problem } from "../types";
import { formatRelativeDate } from "../utils/formatDate";
import "./ProblemCard.css";

export function ProblemCard({
  problem,
  onOpen,
}: {
  problem: Problem;
  onOpen: (problem: Problem) => void;
}) {
  return (
    <article
      className="problem-card"
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
            title="Opportunity score"
          >
            <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M8 2.5l1.8 3.9 4.2.4-3.2 2.9.9 4.3L8 11.9l-3.7 2 .9-4.3-3.2-2.9 4.2-.4L8 2.5z"
                fill="currentColor"
              />
            </svg>
            {problem.signal}
          </span>
        )}
      </div>

      <h3 className="problem-card-title">{problem.title}</h3>
      <p className="problem-card-description">{problem.description}</p>

      <div className="problem-card-meta">
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
    </article>
  );
}
