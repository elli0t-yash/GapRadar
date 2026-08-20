import { useCallback, useEffect, useMemo, useState } from "react";
import { PageShell } from "../components/PageShell";
import { Hero } from "../components/Hero";
import { DockingHeading } from "../components/DockingHeading";
import { ProblemCard } from "../components/ProblemCard";
import { ProblemDetail } from "../components/ProblemDetail";
import { RecentShowcase } from "../components/RecentShowcase";
import { SkeletonCard } from "../components/SkeletonCard";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { NetworkError, isAbort } from "../api/client";
import { listOpportunities } from "../api/opportunities";
import { industriesOf, toProblem } from "../api/adapters";
import type { Problem } from "../types";
import "./GapRadarPage.css";

const FEATURED_COUNT = 6;
const SHOWCASE_COUNT = 8;

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "Could not reach the GapRadar API. Check the backend is running and that this origin is allowed.";
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}

export function GapRadarPage({ introDone = true }: { introDone?: boolean }) {
  const [problems, setProblems] = useState<Problem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const [activeProblem, setActiveProblem] = useState<Problem | null>(null);
  const [cardsRevealed, setCardsRevealed] = useState(false);
  // True once the user has touched search or the category chips, including
  // an explicit click back onto "All" -- that is a deliberate "show
  // everything" choice, not the same as never having filtered at all.
  const [hasFiltered, setHasFiltered] = useState(false);

  // The one place the Discover feed is loaded. `limit=200` is the backend's
  // maximum and returns the whole corpus, so search and industry filtering
  // happen in memory below -- the API exposes no such parameters.
  useEffect(() => {
    const controller = new AbortController();
    setLoadError(null);

    listOpportunities(200, controller.signal)
      .then((opportunities) => setProblems(opportunities.map(toProblem)))
      .catch((error) => {
        if (isAbort(error)) return;
        setProblems(null);
        setLoadError(describeError(error));
      });

    return () => controller.abort();
  }, [attempt]);

  const categories = useMemo(
    () => (problems ? industriesOf(problems) : []),
    [problems],
  );

  // Filtering only. The array arrives ranked best-first from the backend and
  // that order is preserved throughout -- nothing here re-ranks.
  const filteredProblems = useMemo(() => {
    if (!problems) return [];

    const q = query.trim().toLowerCase();
    return problems.filter((p) => {
      const matchesCategory = category === "All" || p.category === category;
      const matchesQuery =
        q.length === 0 ||
        p.title.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        (p.category?.toLowerCase().includes(q) ?? false);
      return matchesCategory && matchesQuery;
    });
  }, [problems, query, category]);

  const handleQueryChange = useCallback((value: string) => {
    setQuery(value);
    setHasFiltered(true);
  }, []);

  const handleCategoryChange = useCallback((value: string) => {
    setCategory(value);
    setHasFiltered(true);
  }, []);

  const resetFilters = useCallback(() => {
    setQuery("");
    setCategory("All");
    setHasFiltered(false);
  }, []);

  const retry = useCallback(() => {
    setProblems(null);
    setAttempt((n) => n + 1);
  }, []);

  const isLoading = problems === null && loadError === null;
  const isFiltering = query.trim().length > 0 || category !== "All";

  // Server order. The featured grid shows every match once the user has
  // touched search or the category chips -- including an explicit click
  // back onto "All" -- so "Choose from N problems" is never a lie. Only the
  // untouched, unfiltered landing state is capped to a curated preview.
  const featuredProblems = useMemo(
    () =>
      hasFiltered
        ? filteredProblems
        : filteredProblems.slice(0, FEATURED_COUNT),
    [filteredProblems, hasFiltered],
  );

  // Server order, the slice after the featured cards so the two sections do
  // not show the same opportunities. Deliberately NOT sorted by date: every
  // opportunity in the current corpus shares one ingestion timestamp, so a
  // date sort would imply a recency ordering the data does not carry.
  const showcaseProblems = useMemo(() => {
    if (hasFiltered) return [];
    const rest = filteredProblems.slice(FEATURED_COUNT);
    const pool = rest.length > 0 ? rest : filteredProblems;
    return pool.slice(0, SHOWCASE_COUNT);
  }, [filteredProblems, hasFiltered]);

  return (
    <PageShell>
      <Hero
        query={query}
        onQueryChange={handleQueryChange}
        category={category}
        onCategoryChange={handleCategoryChange}
        categories={categories}
      />

      <div className="gapradar-content">
        <div className="container">
          <DockingHeading
            ready={introDone}
            onDockComplete={() => setCardsRevealed(true)}
          />

          {cardsRevealed && (
            <section className="gapradar-section">
              <div className="gapradar-section-header">
                <h2>
                  {/* Counted from the feed itself -- never from dashboard
                      signal totals, which include untrusted signals. */}
                  Choose from {problems?.length ?? 0} problems worth solving{" "}
                  <span className="gapradar-stars" aria-hidden="true">
                    ★★★★★
                  </span>
                </h2>
                <p className="gapradar-section-subtitle">
                  Sorted by signal strength
                </p>
              </div>

              <div className="gapradar-grid">
                {isLoading &&
                  Array.from({ length: FEATURED_COUNT }).map((_, i) => (
                    <SkeletonCard key={i} />
                  ))}

                {loadError && (
                  <ErrorState
                    title="Could not load opportunities"
                    message={loadError}
                    onRetry={retry}
                  />
                )}

                {!isLoading &&
                  !loadError &&
                  featuredProblems.length === 0 &&
                  (isFiltering ? (
                    <EmptyState onReset={resetFilters} />
                  ) : (
                    <EmptyState
                      title="No trusted opportunities right now"
                      subtitle="The feed is empty. Nothing is being withheld locally — this is what the backend returned."
                      onReset={resetFilters}
                    />
                  ))}

                {!isLoading &&
                  !loadError &&
                  featuredProblems.map((problem) => (
                    <ProblemCard
                      key={problem.id}
                      problem={problem}
                      onOpen={setActiveProblem}
                    />
                  ))}
              </div>
            </section>
          )}

          <section className="gapradar-section">
            {isLoading && (
              <div className="gapradar-grid">
                {Array.from({ length: 4 }).map((_, i) => (
                  <SkeletonCard key={i} />
                ))}
              </div>
            )}

            {!isLoading && !loadError && showcaseProblems.length > 0 && (
              <RecentShowcase
                problems={showcaseProblems}
                onOpen={setActiveProblem}
              />
            )}
          </section>
        </div>
      </div>

      {activeProblem && (
        <ProblemDetail
          problem={activeProblem}
          onClose={() => setActiveProblem(null)}
        />
      )}
    </PageShell>
  );
}
