import { useRef } from "react";
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
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);

  function scrollBy(amount: number) {
    scrollerRef.current?.scrollBy({ left: amount, behavior: "smooth" });
  }

  return (
    <section className="hero">
      <div className="container hero-inner">
        <h1 className="hero-title">
          Find your
          <br />
          next big problem
        </h1>

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
            placeholder='Try "FinTech"'
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
