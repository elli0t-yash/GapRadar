import { useRef } from "react";
import { Link } from "react-router-dom";
import "./Hero.css";

function ScrollArrow({
  direction,
  onClick,
}: {
  direction: "left" | "right";
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`hero-scroll-arrow hero-scroll-arrow-${direction}`}
      onClick={onClick}
      aria-label={direction === "left" ? "Scroll left" : "Scroll right"}
    >
      <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <path
          d={direction === "left" ? "M12 4l-6 6 6 6" : "M8 4l6 6-6 6"}
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}

export function Hero({
  query,
  onQueryChange,
  category,
  onCategoryChange,
  categories,
  opportunityCount,
}: {
  query: string;
  onQueryChange: (value: string) => void;
  category: string;
  onCategoryChange: (value: string) => void;
  /**
   * Derived from the opportunities actually returned by the API. The backend
   * exposes no industry filter, so the feed on screen is the only honest
   * source for these chips.
   */
  categories: string[];
  opportunityCount: number | null;
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);

  function scrollBy(amount: number) {
    scrollerRef.current?.scrollBy({ left: amount, behavior: "smooth" });
  }

  return (
    <section className="hero">
      <div className="container hero-inner">
        <div className="hero-story">
          <div className="hero-copy">
            <p className="hero-eyebrow">Trust-aware opportunity intelligence</p>
            <h1 className="hero-title">Find what the market is missing.</h1>
            <p className="hero-deck">
              Live market signals become readable, reliability-gated
              opportunities—with the source trail kept intact.
            </p>

            <p className="hero-investigate-prompt">
              Have your own idea?{" "}
              <Link to="/investigate">
                Research it with GapRadar <span aria-hidden="true">→</span>
              </Link>
            </p>

            <dl className="hero-facts" aria-label="Current opportunity feed">
              <div>
                <dt>Trusted opportunities</dt>
                <dd>{opportunityCount === null ? "—" : opportunityCount}</dd>
              </div>
              <div>
                <dt>Markets represented</dt>
                <dd>{opportunityCount === null ? "—" : categories.length}</dd>
              </div>
              <div className="is-trust">
                <dt>Source policy</dt>
                <dd>RecallGuard gated</dd>
              </div>
            </dl>
          </div>

          <div className="hero-radar" aria-hidden="true">
            <span className="hero-radar-ring is-one" />
            <span className="hero-radar-ring is-two" />
            <span className="hero-radar-ring is-three" />
            <span className="hero-radar-axis is-horizontal" />
            <span className="hero-radar-axis is-vertical" />
            <span className="hero-radar-sweep" />
            <span className="hero-radar-dot is-a" />
            <span className="hero-radar-dot is-b" />
            <span className="hero-radar-dot is-c" />
            <span className="hero-radar-core">GR</span>
          </div>
        </div>

        <form
          className="hero-search"
          role="search"
          onSubmit={(e) => e.preventDefault()}
        >
          <svg
            className="hero-search-icon"
            viewBox="0 0 20 20"
            fill="none"
            aria-hidden="true"
          >
            <circle
              cx="9"
              cy="9"
              r="6.5"
              stroke="currentColor"
              strokeWidth="1.6"
            />
            <path
              d="M18 18l-4.35-4.35"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
          <label htmlFor="hero-query" className="visually-hidden">
            Search problems
          </label>
          <input
            id="hero-query"
            type="search"
            placeholder='Search a problem, industry, or market gap'
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
          />
          <button type="submit" className="hero-search-button">
            Search
          </button>
        </form>

        <div className="hero-chips">
          <ScrollArrow direction="left" onClick={() => scrollBy(-240)} />
          <div className="hero-chips-scroller" ref={scrollerRef}>
            <button
              type="button"
              className={
                category === "Top Picks" ? "hero-chip is-active" : "hero-chip"
              }
              onClick={() => onCategoryChange("Top Picks")}
            >
              Top Picks
            </button>
            <button
              type="button"
              className={
                category === "All" ? "hero-chip is-active" : "hero-chip"
              }
              onClick={() => onCategoryChange("All")}
            >
              All
            </button>
            {categories.map((c) => (
              <button
                key={c}
                type="button"
                className={
                  category === c ? "hero-chip is-active" : "hero-chip"
                }
                onClick={() => onCategoryChange(c)}
              >
                {c}
              </button>
            ))}
          </div>
          <ScrollArrow direction="right" onClick={() => scrollBy(240)} />
        </div>
      </div>
    </section>
  );
}
