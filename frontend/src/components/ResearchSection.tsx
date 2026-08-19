import type {
  ResearchEnrichmentStatus,
  ResearchIntelligence,
  ResearchPaperMatch,
  ResearchQueryState,
} from "../api/types";
import {
  completedSearchCount,
  papersSoFar,
  unfinishedSearchCount,
} from "../api/types";
import "./ResearchSection.css";

/**
 * Persisted research intelligence for one opportunity.
 *
 * Everything here is rendered as the backend returned it. In particular the
 * paper list is ALREADY ordered and ALREADY accepted by the backend's own
 * relevance threshold -- this component never sorts it and never re-applies
 * a score cutoff, because that decision is not the frontend's to make.
 */

function formatPublished(date: string): string {
  // A calendar date ("2021-11-05"), not a timestamp -- no time, no zone.
  const parsed = new Date(`${date}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return date;
  return parsed.toLocaleDateString(undefined, {
    month: "short",
    year: "numeric",
  });
}

function authorLine(authors: string[]): string | null {
  if (authors.length === 0) return null;
  if (authors.length <= 3) return authors.join(", ");
  return `${authors.slice(0, 3).join(", ")} et al.`;
}

function PaperCard({ paper }: { paper: ResearchPaperMatch }) {
  const authors = authorLine(paper.authors);

  return (
    <li className="research-paper">
      <div className="research-paper-top">
        <span className="research-paper-relevance" title="Relevance to this problem">
          {Math.round(paper.relevance_score)}
        </span>
        <div className="research-paper-headline">
          <h4 className="research-paper-title">{paper.title}</h4>
          <p className="research-paper-byline">
            {authors && <span>{authors}</span>}
            {authors && <span aria-hidden="true"> · </span>}
            <span>{formatPublished(paper.published_at)}</span>
            <span aria-hidden="true"> · </span>
            <span className="research-paper-arxiv">arXiv:{paper.arxiv_id}</span>
          </p>
        </div>
      </div>

      {/* Pre-truncated by the backend at a word boundary. */}
      <p className="research-paper-abstract">{paper.abstract_preview}</p>

      {/* Null means the matcher had no basis to judge readiness. Saying
          "Not assessed" is the honest render; 0 would be a fabrication. */}
      <div className="research-paper-meta">
        <span className="research-paper-readiness">
          Technical readiness:{" "}
          {paper.technical_readiness_score === null ? (
            <em>Not assessed</em>
          ) : (
            <strong>{Math.round(paper.technical_readiness_score)}</strong>
          )}
        </span>
      </div>

      {paper.match_reason && (
        <p className="research-paper-reason">{paper.match_reason}</p>
      )}

      {paper.matched_concepts.length > 0 && (
        <ul className="research-chips" aria-label="Matched concepts">
          {paper.matched_concepts.map((concept) => (
            <li key={concept} className="research-chip">
              {concept}
            </li>
          ))}
        </ul>
      )}

      <div className="research-paper-links">
        <a href={paper.paper_url} target="_blank" rel="noreferrer">
          Read paper ↗
        </a>
        <a href={paper.pdf_url} target="_blank" rel="noreferrer">
          PDF ↗
        </a>
      </div>
    </li>
  );
}

function ResearchNotice({ title, body }: { title: string; body: string }) {
  return (
    <div className="research-notice">
      <p className="research-notice-title">{title}</p>
      <p className="research-notice-body">{body}</p>
    </div>
  );
}

/**
 * The call to action for an opportunity nobody has analysed yet, and the
 * progress it shows once someone has.
 *
 * The stage list is presented as WHAT THE ANALYSIS DOES, not as a live
 * position within it: the backend reports queued/running/succeeded/failed
 * and nothing finer, so claiming a current stage -- or a percentage --
 * would be inventing detail it cannot prove.
 */
/**
 * One line of the progress list.
 *
 * `detail` is always something the backend actually reported. There is
 * deliberately no timer, no interpolation and no optimistic advance: a
 * stage that cannot be described from persisted state stays "waiting".
 */
function Stage({
  label,
  state,
  detail,
}: {
  label: string;
  state: "done" | "active" | "waiting";
  detail: string;
}) {
  return (
    <li className={`research-stage is-${state}`}>
      <span className="research-stage-mark" aria-hidden="true">
        {state === "done" ? "✓" : state === "active" ? "•" : ""}
      </span>
      <span className="research-stage-label">{label}</span>
      <span className="research-stage-detail">{detail}</span>
    </li>
  );
}

function AnalyseResearchPanel({
  status,
  error,
  queryStates,
  starting,
  onAnalyse,
}: {
  status: ResearchEnrichmentStatus | null;
  error: string | null;
  queryStates: readonly ResearchQueryState[];
  /** A POST is in flight. The button is disabled before any status exists. */
  starting: boolean;
  onAnalyse: () => void;
}) {
  const active = starting || status === "queued" || status === "running";

  if (active) {
    const total = queryStates.length;
    const complete = completedSearchCount(queryStates);
    const unfinished = unfinishedSearchCount(queryStates);
    const papers = papersSoFar(queryStates);
    const searchesDone = total > 0 && complete === total;

    return (
      <div className="research-notice is-working" aria-live="polite">
        <p className="research-notice-title">
          <span className="research-spinner" aria-hidden="true" />
          Analysing the research frontier…
        </p>
        <ul className="research-stages">
          <Stage
            label="Research directions"
            state={total > 0 ? "done" : "active"}
            detail={total > 0 ? `${total} generated` : "generating…"}
          />
          <Stage
            label="Research search"
            state={searchesDone ? "done" : total > 0 ? "active" : "waiting"}
            detail={total > 0 ? `${complete}/${total} complete` : "waiting"}
          />
          <Stage
            label="Semantic matching"
            state={searchesDone ? "active" : "waiting"}
            detail={searchesDone ? `${papers} papers` : "waiting"}
          />
          <Stage
            label="Saving intelligence"
            state="waiting"
            detail="waiting"
          />
        </ul>
        {unfinished > 0 && (
          <p className="research-notice-warning">
            {unfinished} search{unfinished === 1 ? "" : "es"} did not return —
            continuing with the papers already found.
          </p>
        )}
        <p className="research-notice-body">
          You can keep browsing — the results are saved either way.
        </p>
      </div>
    );
  }

  if (status === "failed") {
    return (
      <div className="research-notice is-error">
        <p className="research-notice-title">Research analysis did not complete</p>
        <p className="research-notice-body">
          {error ?? "The analysis could not be completed."}
        </p>
        <button
          type="button"
          className="research-retry"
          onClick={onAnalyse}
          disabled={starting}
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="research-notice is-cta">
      <p className="research-notice-title">Explore matching research</p>
      <p className="research-notice-body">
        GapRadar can search emerging research and identify technologies that may
        help solve this problem.
      </p>
      <button
        type="button"
        className="research-analyse"
        onClick={onAnalyse}
        disabled={starting}
      >
        Analyse research
      </button>
      <p className="research-notice-footnote">
        Matching research has not been analysed for this opportunity yet.
      </p>
    </div>
  );
}


export function ResearchSection({
  research,
  loading,
  error,
  onRetry,
  enrichmentStatus,
  enrichmentError,
  enrichmentQueryStates,
  enrichmentStarting,
  enrichmentWarning,
  onAnalyse,
}: {
  research: ResearchIntelligence | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /**
   * The backend job's state, or null when analysis has never been asked
   * for. Purely reflected -- this component never starts work itself.
   */
  enrichmentStatus: ResearchEnrichmentStatus | null;
  enrichmentError: string | null;
  /** Backend-reported per-query progress. Never synthesised here. */
  enrichmentQueryStates: readonly ResearchQueryState[];
  /** A POST is in flight, so the CTA must not accept another click. */
  enrichmentStarting: boolean;
  /** A partial run: research is real, and so is the gap. */
  enrichmentWarning: string | null;
  /** Runs ONLY from the button below. Never from an effect. */
  onAnalyse: () => void;
}) {
  return (
    <section className="research-section" aria-labelledby="research-heading">
      <h3 id="research-heading" className="research-heading">
        Research intelligence
      </h3>

      {loading && (
        <div className="research-skeleton" aria-busy="true" aria-live="polite">
          <span className="research-skeleton-line" />
          <span className="research-skeleton-line is-short" />
        </div>
      )}

      {/* Scoped to this section: a research failure never takes the rest of
          the opportunity detail down with it. */}
      {!loading && error && (
        <div className="research-notice is-error">
          <p className="research-notice-title">Research could not be loaded</p>
          <p className="research-notice-body">{error}</p>
          <button type="button" className="research-retry" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}

      {!loading && !error && research && (
        <>
          {/* A run that returned real research but not all of it. Shown
              ABOVE the results and never instead of them: the papers
              below are genuine, and the gap is stated rather than hidden
              behind a success. */}
          {enrichmentWarning && (
            <p className="research-partial-warning" role="status">
              {enrichmentWarning}
            </p>
          )}

          {/* State 3: enrichment has never run. `generated_queries` empty is
              the backend's own signal for this -- see the integration doc.
              This is the normal state for almost every opportunity, so it
              offers the action rather than reading as a dead end. */}
          {research.generated_queries.length === 0 &&
            research.matched_paper_count === 0 && (
              <AnalyseResearchPanel
                status={enrichmentStatus}
                error={enrichmentError}
                queryStates={enrichmentQueryStates}
                starting={enrichmentStarting}
                onAnalyse={onAnalyse}
              />
            )}

          {/* State 2: enrichment ran, nothing cleared the backend's bar. */}
          {research.generated_queries.length > 0 &&
            research.matched_paper_count === 0 && (
              <ResearchNotice
                title="We searched for related research but found nothing relevant enough yet."
                body={`${research.paper_count} paper${
                  research.paper_count === 1 ? "" : "s"
                } were reviewed and none met the relevance bar.`}
              />
            )}

          {/* State 1: enriched, with accepted matches. */}
          {research.matched_paper_count > 0 && (
            <>
              <dl className="research-stats">
                <div className="research-stat">
                  <dt>Papers discovered</dt>
                  <dd>{research.paper_count}</dd>
                </div>
                <div className="research-stat">
                  <dt>Relevant papers</dt>
                  <dd>{research.matched_paper_count}</dd>
                </div>
                {/* Null average means no matches; hidden rather than shown as 0. */}
                {research.average_relevance_score !== null && (
                  <div className="research-stat">
                    <dt>Avg. relevance</dt>
                    <dd>{Math.round(research.average_relevance_score)}</dd>
                  </div>
                )}
              </dl>

              {research.top_concepts.length > 0 && (
                <ul className="research-chips is-topics" aria-label="Top concepts">
                  {research.top_concepts.map((concept) => (
                    <li key={concept} className="research-chip is-topic">
                      {concept}
                    </li>
                  ))}
                </ul>
              )}

              <ul className="research-papers">
                {research.top_papers.map((paper) => (
                  <PaperCard key={paper.research_paper_id} paper={paper} />
                ))}
              </ul>

              {research.matched_paper_count > research.top_papers.length && (
                <p className="research-footnote">
                  Showing the top {research.top_papers.length} of{" "}
                  {research.matched_paper_count} relevant papers.
                </p>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
