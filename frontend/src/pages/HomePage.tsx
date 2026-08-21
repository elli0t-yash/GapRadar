import { Link } from "react-router-dom";
import { PageShell } from "../components/PageShell";
import { HomeHero } from "../components/home/HomeHero";
import { SignalConvergence } from "../components/home/SignalConvergence";
import { FeaturedOpportunity } from "../components/home/FeaturedOpportunity";
import { TrustStory } from "../components/home/TrustStory";
import { InvestigationPrompt } from "../components/home/InvestigationPrompt";
import { InvestigationFlow } from "../components/home/InvestigationFlow";
import { CompletedInvestigation } from "../components/home/CompletedInvestigation";
import { InterfacesShowcase } from "../components/home/InterfacesShowcase";
import { Reveal } from "../components/home/Reveal";
import { useOpportunityFeed } from "../hooks/useHomeData";
import "./HomePage.css";

/**
 * The public story.
 *
 * This route is deliberately NOT the product: the intelligence workspace
 * lives at /opportunities, /investigate, /investigations/:id and
 * /reliability, and stays information-dense. What the two share is the
 * token layer, so the story and the product read as one thing.
 *
 * The feed is loaded once here and passed down, so the hero readout, the
 * convergence outcome, the featured opportunity, and the CLI rows are all
 * the same live data rather than four separate requests.
 */
export function HomePage() {
  const feed = useOpportunityFeed();

  return (
    <PageShell>
      <div className="home">
        <HomeHero feed={feed} />
        <SignalConvergence feed={feed} />
        <FeaturedOpportunity feed={feed} />
        <TrustStory />
        <InvestigationPrompt />
        <InvestigationFlow />
        <CompletedInvestigation />
        <InterfacesShowcase feed={feed} />

        <section className="home-section home-closing" aria-labelledby="home-closing-title">
          <div className="container">
            <Reveal className="closing-inner">
              <h2 id="home-closing-title" className="home-display">
                <span>Find what the market</span>
                <span>is missing.</span>
              </h2>
              <p className="home-lede">
                Discover opportunities automatically, or investigate your own
                hypothesis.
              </p>
              <div className="home-actions">
                <Link className="home-button is-primary" to="/opportunities">
                  Explore opportunities
                </Link>
                <Link className="home-button" to="/investigate">
                  Investigate an idea
                </Link>
              </div>
              <p className="closing-note">
                Find what the market is missing — and know why you believe it.
              </p>
            </Reveal>
          </div>
        </section>
      </div>
    </PageShell>
  );
}
