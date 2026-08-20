import { useCallback, useEffect, useMemo, useState } from "react";
import { PageShell } from "../components/PageShell";
import { Hero } from "../components/Hero";
import { ProblemCard } from "../components/ProblemCard";
import { ProblemDetail } from "../components/ProblemDetail";
import { SkeletonCard } from "../components/SkeletonCard";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { ApiError, NetworkError, isAbort } from "../api/client";
import { listOpportunities } from "../api/opportunities";
import { industriesOf, toProblem } from "../api/adapters";
import type { Problem } from "../types";
import "./GapRadarPage.css";

const FEATURED_COUNT = 6;

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "We couldn't connect to GapRadar. Check your connection and try again.";
  }
  if (error instanceof ApiError) {
    return error.status >= 500
      ? "GapRadar couldn't prepare the opportunity feed just now."
      : error.message;
  }
  return "We couldn't load this intelligence.";
}

export function GapRadarPage() {
  const [problems, setProblems] = useState<Problem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const [activeProblem, setActiveProblem] = useState<Problem | null>(null);
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

  return (
    <PageShell>
      <Hero
        query={query}
        onQueryChange={handleQueryChange}
        category={category}
        onCategoryChange={handleCategoryChange}
        categories={categories}
        opportunityCount={problems?.length ?? null}
      />

      <div className="gapradar-content">
        <div className="container">
          <section className="gapradar-section" aria-labelledby="opportunity-feed-title">
              <div className="gapradar-section-header">
                <p className="gapradar-section-eyebrow">Opportunity radar</p>
                <h2 id="opportunity-feed-title">Where are the strongest market gaps?</h2>
                <p className="gapradar-section-subtitle">
                  Ranked by the opportunity score returned by GapRadar. The first
                  result is the strongest trusted opportunity in this view.
                </p>
              </div>

              <div className="gapradar-grid">
                {isLoading ? (
                  <p className="visually-hidden" role="status">
                    Loading trusted opportunities…
                  </p>
                ) : null}
                {isLoading &&
                  Array.from({ length: FEATURED_COUNT }).map((_, i) => (
                    <SkeletonCard key={i} />
                  ))}

                {loadError && (
                  <ErrorState
                    title="We couldn't load this intelligence"
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
                      subtitle="GapRadar has no reliability-gated opportunities to show in this feed yet. A valid zero state is not an error."
                    />
                  ))}

                {!isLoading &&
                  !loadError &&
                  featuredProblems.map((problem, index) => (
                    <ProblemCard
                      key={problem.id}
                      problem={problem}
                      onOpen={setActiveProblem}
                      rank={index + 1}
                      featured={index === 0}
                    />
                  ))}
              </div>
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
