import { Link } from "react-router-dom";
import { Reveal } from "./Reveal";
import type { OpportunityFeed } from "../../hooks/useHomeData";

/**
 * ONE opportunity, at full size, from the live ranked feed.
 *
 * Every value on screen is a field the API returned. Nothing is derived,
 * nothing is invented: no demand percentage, no confidence, no market size.
 * A null `signal` renders as "Not scorable" because that is a real backend
 * answer and is not zero.
 */
export function FeaturedOpportunity({ feed }: { feed: OpportunityFeed | null }) {
  const strongest = feed?.problems[0] ?? null;
  const runnersUp = feed?.problems.slice(1, 4) ?? [];

  return (
    <section className="home-section home-featured" aria-labelledby="home-featured-title">
      <div className="container">
        <Reveal className="home-section-head">
          <p className="home-eyebrow">Opportunity intelligence</p>
          <h2 id="home-featured-title" className="home-section-title">
            From signal to opportunity.
          </h2>
          <p className="home-lede">
            The feed arrives ranked. This is the strongest opportunity GapRadar
            is holding right now, exactly as the product shows it.
          </p>
        </Reveal>

        {strongest === null ? (
          <div className="featured-slab is-loading" aria-hidden="true">
            <span className="featured-skeleton is-rank" />
            <span className="featured-skeleton is-title" />
            <span className="featured-skeleton is-title is-short" />
            <span className="featured-skeleton is-body" />
            <span className="featured-skeleton is-body is-short" />
          </div>
        ) : (
          <Reveal className="featured-slab">
            <div className="featured-main">
              <p className="featured-rank" aria-label="Rank 1">
                <span aria-hidden="true">01</span>
              </p>

              <p className="featured-industry">
                {strongest.category ?? "Uncategorised"}
              </p>

              <h3 className="featured-title">{strongest.title}</h3>

              <p className="featured-description">{strongest.description}</p>

              <div className="featured-provenance">
                <span className="featured-trust">
                  <span aria-hidden="true">✓</span> Reliability-gated source
                </span>
                {strongest.source ? (
                  <span className="featured-source">{strongest.source}</span>
                ) : null}
                {strongest.sourceUrl ? (
                  <a href={strongest.sourceUrl} target="_blank" rel="noreferrer">
                    Open the original signal{" "}
                    <span aria-hidden="true">↗</span>
                  </a>
                ) : null}
              </div>
            </div>

            <aside className="featured-score">
              {strongest.signal === null ? (
                <>
                  <p className="featured-score-value is-unscored">Not scorable</p>
                  <p className="featured-score-label">
                    GapRadar returned no score for this signal. That is an
                    answer, not a zero.
                  </p>
                </>
              ) : (
                <>
                  <p className="featured-score-value">{strongest.signal}</p>
                  <p className="featured-score-label">Opportunity score</p>
                </>
              )}

              <Link className="featured-cta" to="/opportunities">
                Explore opportunity <span aria-hidden="true">→</span>
              </Link>
            </aside>
          </Reveal>
        )}

        {runnersUp.length > 0 ? (
          <Reveal className="featured-runners" delay={0.08}>
            <p className="featured-runners-label">Next in the ranking</p>
            <ol>
              {runnersUp.map((problem, index) => (
                <li key={problem.id}>
                  <span className="featured-runners-rank" aria-hidden="true">
                    {String(index + 2).padStart(2, "0")}
                  </span>
                  <span className="featured-runners-title">{problem.title}</span>
                  <span className="featured-runners-score">
                    {problem.signal === null ? "—" : problem.signal}
                  </span>
                </li>
              ))}
            </ol>
          </Reveal>
        ) : null}
      </div>
    </section>
  );
}
