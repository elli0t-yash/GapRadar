import type {
  CompetitorClassification,
  CompetitorCollection,
  CompetitorEvidence,
} from "../api/investigationTypes";
import "./InvestigationEvidenceSections.css";

const GROUPS: ReadonlyArray<{
  classification: CompetitorClassification;
  label: string;
  description: string;
}> = [
  {
    classification: "direct",
    label: "Direct candidates",
    description: "Products presented as addressing substantially the same need.",
  },
  {
    classification: "adjacent",
    label: "Adjacent candidates",
    description: "Products operating near this problem or serving overlapping users.",
  },
  {
    classification: "substitute",
    label: "Substitutes",
    description: "Different approaches a user could choose instead.",
  },
];

function Provenance({ queries }: { queries: string[] }) {
  if (queries.length <= 1) return null;
  return (
    <details className="evidence-provenance">
      <summary>Found across {queries.length} search directions</summary>
      <ul>
        {queries.map((query) => (
          <li key={query}>{query}</li>
        ))}
      </ul>
    </details>
  );
}

function CompetitorCard({ competitor }: { competitor: CompetitorEvidence }) {
  return (
    <li className={`evidence-card is-${competitor.classification}`}>
      <div className="evidence-card-topline">
        <span className="evidence-classification">{competitor.classification}</span>
        <span className="evidence-score">
          Relevance {Math.round(competitor.relevance_score)}
        </span>
      </div>
      <h4>{competitor.name}</h4>
      <p className="evidence-domain">{competitor.domain}</p>
      <p className="evidence-snippet">{competitor.snippet}</p>
      <p className="evidence-reason">{competitor.reason}</p>
      <Provenance queries={competitor.provenance.found_by_queries} />
      <a href={competitor.url} target="_blank" rel="noreferrer">
        View source ↗
      </a>
    </li>
  );
}

export function CompetitorSection({
  collection,
  partial,
}: {
  collection: CompetitorCollection;
  partial: boolean;
}) {
  const visibleCount = GROUPS.reduce(
    (total, group) =>
      total +
      collection.competitors.filter(
        (competitor) => competitor.classification === group.classification,
      ).length,
    0,
  );

  return (
    <section className="investigation-result-section evidence-section" aria-labelledby="competitor-candidates-title">
      <header className="investigation-result-heading">
        <div>
          <p className="investigation-result-eyebrow">Product discovery</p>
          <h2 id="competitor-candidates-title">Competitor candidates</h2>
        </div>
      </header>

      {partial ? (
        <p className="investigation-result-warning">
          Competitor discovery was partial. These candidates are the persisted
          results from the searches that succeeded.
        </p>
      ) : null}

      {visibleCount === 0 ? (
        <div className="investigation-result-empty">
          <h3>No competitor candidates were accepted from this discovery pass.</h3>
        </div>
      ) : null}

      {GROUPS.map((group) => {
        const competitors = collection.competitors.filter(
          (competitor) => competitor.classification === group.classification,
        );
        if (competitors.length === 0) return null;

        return (
          <section
            key={group.classification}
            className={`evidence-group is-${group.classification}`}
            aria-labelledby={`competitor-${group.classification}-title`}
          >
            <header>
              <div>
                <h3 id={`competitor-${group.classification}-title`}>{group.label}</h3>
                <p>{group.description}</p>
              </div>
              <span>{competitors.length}</span>
            </header>
            <ul className="evidence-card-list">
              {competitors.map((competitor) => (
                <CompetitorCard key={competitor.id} competitor={competitor} />
              ))}
            </ul>
          </section>
        );
      })}
    </section>
  );
}
