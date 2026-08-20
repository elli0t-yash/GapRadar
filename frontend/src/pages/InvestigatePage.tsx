import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { isAbort, NetworkError } from "../api/client";
import { createInvestigation, listInvestigations } from "../api/investigations";
import type { Investigation } from "../api/investigationTypes";
import { ErrorState } from "../components/ErrorState";
import { PageShell } from "../components/PageShell";
import "./InvestigatePage.css";

function describeError(error: unknown): string {
  if (error instanceof NetworkError) {
    return "Could not reach the GapRadar API. Start the backend and check its CORS settings.";
  }
  return error instanceof Error ? error.message : "The request could not be completed.";
}

function displayLabel(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatCreatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function InvestigatePage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState("");
  const [queryError, setQueryError] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [investigations, setInvestigations] = useState<Investigation[] | null>(
    null,
  );
  const [listError, setListError] = useState<string | null>(null);
  const [listAttempt, setListAttempt] = useState(0);
  const createInFlightRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setListError(null);

    listInvestigations(undefined, controller.signal)
      .then(setInvestigations)
      .catch((error) => {
        if (isAbort(error)) return;
        setInvestigations(null);
        setListError(describeError(error));
      });

    return () => controller.abort();
  }, [listAttempt]);

  const retryList = useCallback(() => {
    setInvestigations(null);
    setListAttempt((attempt) => attempt + 1);
  }, []);

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (createInFlightRef.current) return;

      const normalizedQuery = query.trim();
      const normalizedIndustry = industry.trim();
      if (!normalizedQuery) {
        setQueryError("Tell GapRadar what you want to investigate.");
        return;
      }

      createInFlightRef.current = true;
      setCreating(true);
      setQueryError(null);
      setCreateError(null);

      try {
        const investigation = await createInvestigation({
          query: normalizedQuery,
          ...(normalizedIndustry ? { industry: normalizedIndustry } : {}),
        });
        navigate(`/investigations/${investigation.id}`);
      } catch (error) {
        if (!isAbort(error)) setCreateError(describeError(error));
      } finally {
        createInFlightRef.current = false;
        setCreating(false);
      }
    },
    [industry, navigate, query],
  );

  return (
    <PageShell>
      <div className="investigate-page">
        <div className="container">
          <header className="investigate-hero">
            <p className="investigate-eyebrow">Evidence workspace</p>
            <h1>Investigate an idea from multiple directions.</h1>
            <p>
              Start with a hypothesis. Creating it saves the workspace; analysis
              begins only from the detail page when you explicitly ask.
            </p>
          </header>

          <section className="investigate-create" aria-labelledby="create-investigation-title">
            <div className="investigate-section-heading">
              <p className="investigate-step">New investigation</p>
              <h2 id="create-investigation-title">
                What idea, problem, or market do you want GapRadar to investigate?
              </h2>
            </div>

            <form
              className="investigate-form"
              noValidate
              onSubmit={(event) => void handleSubmit(event)}
            >
              <div className="investigate-field-group">
                <label className="investigate-field">
                  <span>Idea or problem</span>
                  <textarea
                    value={query}
                    onChange={(event) => {
                      setQuery(event.target.value);
                      if (queryError) setQueryError(null);
                    }}
                    aria-invalid={queryError !== null}
                    aria-describedby={queryError ? "investigation-query-error" : undefined}
                    placeholder="AI demand forecasting for independent restaurants"
                    rows={4}
                    required
                  />
                </label>
                {queryError ? (
                  <p id="investigation-query-error" className="investigate-field-error">
                    {queryError}
                  </p>
                ) : null}
              </div>

              <div className="investigate-field-group">
                <label className="investigate-field">
                  <span>
                    Industry <em>Optional</em>
                  </span>
                  <input
                    type="text"
                    value={industry}
                    onChange={(event) => setIndustry(event.target.value)}
                    placeholder="Restaurants"
                  />
                </label>
              </div>

              {createError ? (
                <p className="investigate-form-error" role="alert">
                  {createError}
                </p>
              ) : null}

              <div className="investigate-form-footer">
                <p>No analysis or provider request runs when this workspace is created.</p>
                <button type="submit" disabled={creating}>
                  {creating ? "Creating…" : "Create Investigation"}
                </button>
              </div>
            </form>
          </section>

          <section className="investigate-recents" aria-labelledby="recent-investigations-title">
            <div className="investigate-section-heading">
              <p className="investigate-step">Saved workspaces</p>
              <h2 id="recent-investigations-title">Recent investigations</h2>
            </div>

            {investigations === null && listError === null ? (
              <div className="investigation-card-grid" aria-label="Loading investigations">
                {Array.from({ length: 3 }).map((_, index) => (
                  <div key={index} className="investigation-card-skeleton" />
                ))}
              </div>
            ) : null}

            {listError ? (
              <ErrorState
                title="Could not load investigations"
                message={listError}
                onRetry={retryList}
              />
            ) : null}

            {investigations?.length === 0 ? (
              <div className="investigation-list-empty">
                <h3>No investigations yet.</h3>
                <p>Your saved idea workspaces will appear here.</p>
              </div>
            ) : null}

            {investigations && investigations.length > 0 ? (
              <div className="investigation-card-grid">
                {investigations.map((investigation) => (
                  <Link
                    key={investigation.id}
                    className="investigation-card"
                    to={`/investigations/${investigation.id}`}
                  >
                    <div
                      className={`investigation-card-topline${
                        investigation.industry ? "" : " is-status-only"
                      }`}
                    >
                      {investigation.industry ? (
                        <span>{investigation.industry}</span>
                      ) : null}
                      <strong>{displayLabel(investigation.status)}</strong>
                    </div>
                    <h3>{investigation.query}</h3>
                    <div className="investigation-card-footer">
                      <time dateTime={investigation.created_at}>
                        {formatCreatedAt(investigation.created_at)}
                      </time>
                      <span aria-hidden="true">→</span>
                    </div>
                  </Link>
                ))}
              </div>
            ) : null}
          </section>
        </div>
      </div>
    </PageShell>
  );
}
