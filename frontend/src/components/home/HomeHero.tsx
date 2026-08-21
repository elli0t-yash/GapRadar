import { Link } from "react-router-dom";
import { HomeRadar } from "./HomeRadar";
import type { OpportunityFeed } from "../../hooks/useHomeData";

export function HomeHero({ feed }: { feed: OpportunityFeed | null }) {
  return (
    <section className="home-hero" aria-labelledby="home-hero-title">
      <div className="container home-hero-inner">
        <div className="home-hero-copy">
          <p className="home-eyebrow">Trust-aware opportunity intelligence</p>

          <h1 id="home-hero-title" className="home-display">
            <span>Find what the market</span>
            <span>is missing.</span>
          </h1>

          <p className="home-lede">
            GapRadar turns live web signals into evidence-backed market
            opportunities — and shows you why they deserve attention.
          </p>

          <div className="home-actions">
            <Link className="home-button is-primary" to="/opportunities">
              Explore opportunities
            </Link>
            <Link className="home-button" to="/investigate">
              Investigate an idea
            </Link>
          </div>

          <p className="home-hero-modes">
            Discovery Mode
            <span aria-hidden="true">·</span>
            Investigation Mode
            <span aria-hidden="true">·</span>
            Trust-aware extraction
          </p>
        </div>

        <HomeRadar
          opportunityCount={feed?.problems.length ?? null}
          marketCount={feed?.markets.length ?? null}
        />
      </div>
    </section>
  );
}
