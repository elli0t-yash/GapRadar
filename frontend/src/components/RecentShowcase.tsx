import type { Problem } from "../types";
import { formatRelativeDate } from "../utils/formatDate";
import "./RecentShowcase.css";

const CARD_TONES = ["tone-orange", "tone-deep", "tone-orange", "tone-deep"];

function Stars({ count }: { count: number }) {
  return (
    <span className="stars" aria-hidden="true">
      {"★".repeat(count)}
      {"☆".repeat(5 - count)}
    </span>
  );
}

export function RecentShowcase({
  problems,
  onOpen,
}: {
  problems: Problem[];
  onOpen: (problem: Problem) => void;
}) {
  // Rendered twice back-to-back so the marquee can loop seamlessly at -50%.
  const marqueeCards = [...problems, ...problems];
  const marqueeDuration = `${Math.max(problems.length * 4, 20)}s`;

  return (
    <div className="recent-showcase">
      <div className="recent-showcase-intro">
        <span className="recent-showcase-stars" aria-hidden="true">
          ★★★★★
        </span>
        <h2>The Perfect Match</h2>
        <p>
          Every problem here was sourced and scored, not{" "}
          <strong>guessed</strong>.
        </p>
      </div>

      <div className="recent-showcase-scroller">
        <div
          className="recent-showcase-track"
          style={{ "--marquee-duration": marqueeDuration } as React.CSSProperties}
        >
          {marqueeCards.map((problem, i) => (
            <button
              type="button"
              key={`${problem.id}-${i}`}
              className={`recent-showcase-card ${CARD_TONES[i % CARD_TONES.length]}`}
              onClick={() => onOpen(problem)}
              tabIndex={i < problems.length ? 0 : -1}
              aria-hidden={i < problems.length ? undefined : true}
            >
              <span className="recent-showcase-header">
                <span className="recent-showcase-avatar" aria-hidden="true">
                  {(problem.category ?? "—").charAt(0)}
                </span>
                <span className="recent-showcase-who">
                  <span className="recent-showcase-name">
                    {problem.category ?? "Uncategorised"}
                  </span>
                  <span className="recent-showcase-role">
                    {problem.source ?? "Unknown source"}
                  </span>
                </span>
              </span>

              <p className="recent-showcase-title">{problem.title}</p>

              <span className="recent-showcase-pill">
                {formatRelativeDate(problem.date)}
                {problem.signal !== null && (
                  <Stars count={Math.round((problem.signal / 100) * 5) || 1} />
                )}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
