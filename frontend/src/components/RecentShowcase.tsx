import { useEffect, useRef, useState } from "react";
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
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(true);

  function updateScrollState() {
    const el = scrollerRef.current;
    if (!el) return;
    setCanScrollLeft(el.scrollLeft > 4);
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 4);
  }

  function scrollBy(amount: number) {
    scrollerRef.current?.scrollBy({ left: amount, behavior: "smooth" });
  }

  useEffect(() => {
    updateScrollState();
  }, [problems]);

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
        <div className="recent-showcase-arrows">
          <button
            type="button"
            className="recent-showcase-arrow"
            onClick={() => scrollBy(-320)}
            disabled={!canScrollLeft}
            aria-label="Scroll left"
          >
            <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="M12 4l-6 6 6 6"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          <button
            type="button"
            className="recent-showcase-arrow recent-showcase-arrow-solid"
            onClick={() => scrollBy(320)}
            disabled={!canScrollRight}
            aria-label="Scroll right"
          >
            <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path
                d="M8 4l6 6-6 6"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>

      <div
        className="recent-showcase-scroller"
        ref={scrollerRef}
        onScroll={updateScrollState}
      >
        {problems.map((problem, i) => (
          <button
            type="button"
            key={problem.id}
            className={`recent-showcase-card ${CARD_TONES[i % CARD_TONES.length]}`}
            onClick={() => onOpen(problem)}
          >
            <span className="recent-showcase-header">
              <span className="recent-showcase-avatar" aria-hidden="true">
                {problem.category.charAt(0)}
              </span>
              <span className="recent-showcase-who">
                <span className="recent-showcase-name">
                  {problem.category}
                </span>
                <span className="recent-showcase-role">
                  {problem.source}
                </span>
              </span>
            </span>

            <p className="recent-showcase-title">{problem.title}</p>

            <span className="recent-showcase-pill">
              {formatRelativeDate(problem.date)}
              <Stars count={Math.round((problem.signal / 100) * 5) || 1} />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
