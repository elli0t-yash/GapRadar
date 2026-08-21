import type {
  InvestigationPaperMatch,
  InvestigationResearchIntelligence,
} from "../api/investigationTypes";
import { InvestigationResultSection } from "./InvestigationResultSection";
import "./InvestigationResearchSection.css";

function formatPublished(date: string): string {
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

function PaperCard({ paper }: { paper: InvestigationPaperMatch }) {
  const authors = authorLine(paper.authors);

  return (
    <li className="investigation-paper-card">
      <div className="investigation-paper-heading">
        <div>
          <h3>{paper.title}</h3>
          <p className="investigation-paper-byline">
            {authors ? <span>{authors}</span> : null}
            {authors ? <span aria-hidden="true"> · </span> : null}
            <time dateTime={paper.published_at}>
              {formatPublished(paper.published_at)}
            </time>
          </p>
        </div>
        <span className="investigation-score" title="Relevance to this hypothesis">
          {Math.round(paper.relevance_score)} relevance
        </span>
      </div>

      {paper.match_reason ? (
        <div className="investigation-paper-reason">
          <strong>Why GapRadar matched this</strong>
          <p>{paper.match_reason}</p>
        </div>
      ) : null}

      <div className="investigation-paper-meta">
        {paper.technical_readiness_score !== null ? (
          <span>
            Technical readiness: <strong>{Math.round(paper.technical_readiness_score)}</strong>
          </span>
        ) : null}
        <span>arXiv:{paper.arxiv_id}</span>
      </div>

      {paper.matched_concepts.length > 0 ? (
        <ul className="investigation-concept-list" aria-label="Matched concepts">
          {paper.matched_concepts.map((concept) => (
            <li key={concept}>{concept}</li>
          ))}
        </ul>
      ) : null}

      <a
        className="investigation-source-link"
        href={paper.paper_url}
        target="_blank"
        rel="noreferrer"
      >
        Read paper ↗
      </a>
    </li>
  );
}

export function InvestigationResearchSection({
  research,
  partial,
}: {
  research: InvestigationResearchIntelligence;
  partial: boolean;
}) {
  return (
    <InvestigationResultSection
      id="investigation-research-title"
      eyebrow="Research frontier"
      title="Relevant academic research"
    >
      {partial ? (
        <p className="investigation-result-warning">
          Research discovery was partial. These are the persisted matches from
          the searches that completed.
        </p>
      ) : null}

      <dl className="investigation-research-metrics">
        <div>
          <dt>Papers discovered</dt>
          <dd>{research.paper_count}</dd>
        </div>
        <div>
          <dt>Relevant papers</dt>
          <dd>{research.matched_paper_count}</dd>
        </div>
        {research.average_relevance_score !== null ? (
          <div>
            <dt>Average relevance</dt>
            <dd>{Math.round(research.average_relevance_score)}</dd>
          </div>
        ) : null}
      </dl>

      {research.top_concepts.length > 0 ? (
        <ul className="investigation-concept-list is-summary" aria-label="Top concepts">
          {research.top_concepts.map((concept) => (
            <li key={concept}>{concept}</li>
          ))}
        </ul>
      ) : null}

      {research.matched_paper_count === 0 ? (
        <div className="investigation-result-empty">
          <h3>No research crossed the relevance threshold.</h3>
          <p>The scan completed without an accepted academic match.</p>
        </div>
      ) : null}

      {research.top_papers.length > 0 ? (
        <ul className="investigation-paper-list">
          {research.top_papers.map((paper) => (
            <PaperCard key={paper.research_paper_id} paper={paper} />
          ))}
        </ul>
      ) : null}

      {research.matched_paper_count > research.top_papers.length ? (
        <p className="investigation-result-footnote">
          Showing {research.top_papers.length} of {research.matched_paper_count} relevant papers.
        </p>
      ) : null}
    </InvestigationResultSection>
  );
}
