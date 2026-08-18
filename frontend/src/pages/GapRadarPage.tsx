import { useEffect, useMemo, useState } from "react";
import { PageShell } from "../components/PageShell";
import { Hero } from "../components/Hero";
import { DockingHeading } from "../components/DockingHeading";
import { ProblemCard } from "../components/ProblemCard";
import { ProblemDetail } from "../components/ProblemDetail";
import { RecentShowcase } from "../components/RecentShowcase";
import { SkeletonCard } from "../components/SkeletonCard";
import { EmptyState } from "../components/EmptyState";
import { problems as allProblems } from "../data/problems";
import type { Problem } from "../types";
import "./GapRadarPage.css";

const FEATURED_COUNT = 6;
const RECENT_COUNT = 8;

async function fetchProblems(): Promise<Problem[]> {
  await new Promise((resolve) => setTimeout(resolve, 650));
  return allProblems;
}

export function GapRadarPage({ introDone = true }: { introDone?: boolean }) {
  const [problems, setProblems] = useState<Problem[] | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const [activeProblem, setActiveProblem] = useState<Problem | null>(null);
  const [cardsRevealed, setCardsRevealed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchProblems().then((data) => {
      if (!cancelled) setProblems(data);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const filteredProblems = useMemo(() => {
    if (!problems) return [];

    const q = query.trim().toLowerCase();
    return problems.filter((p) => {
      const matchesCategory = category === "All" || p.category === category;
      const matchesQuery =
        q.length === 0 ||
        p.title.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.category.toLowerCase().includes(q);
      return matchesCategory && matchesQuery;
    });
  }, [problems, query, category]);

  const featuredProblems = useMemo(
    () =>
      [...filteredProblems]
        .sort((a, b) => b.signal - a.signal)
        .slice(0, FEATURED_COUNT),
    [filteredProblems],
  );

  const recentProblems = useMemo(
    () =>
      [...filteredProblems]
        .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
        .slice(0, RECENT_COUNT),
    [filteredProblems],
  );

  function resetFilters() {
    setQuery("");
    setCategory("All");
  }

  const isLoading = problems === null;

  return (
    <PageShell>
      <Hero
        query={query}
        onQueryChange={setQuery}
        category={category}
        onCategoryChange={setCategory}
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
                  Choose from {allProblems.length} problems worth solving{" "}
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

                {!isLoading && featuredProblems.length === 0 && (
                  <EmptyState onReset={resetFilters} />
                )}

                {!isLoading &&
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

            {!isLoading && recentProblems.length === 0 && (
              <EmptyState onReset={resetFilters} />
            )}

            {!isLoading && recentProblems.length > 0 && (
              <RecentShowcase
                problems={recentProblems}
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
