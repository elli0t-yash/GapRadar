import type {
  DemandClassification,
  DemandEvidence,
  DemandEvidenceCollection,
} from "../api/investigationTypes";
import "./InvestigationEvidenceSections.css";

const GROUPS: ReadonlyArray<{
  classification: DemandClassification;
  label: string;
  description: string;
}> = [
  {
    classification: "contradicts",
    label: "Contradicting evidence",
    description: "Sources that challenge the hypothesis or point in another direction.",
  },
  {
    classification: "strong_support",
    label: "Strong support",
    description: "Sources that strongly reinforce the stated problem or demand.",
  },
  {
    classification: "support",
    label: "Supporting evidence",
    description: "Sources that add useful support without making the strongest case.",
  },
];

function formatPublished(value: string): string {
  const date = new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00` : value,
  );
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

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

function EvidenceCard({ evidence }: { evidence: DemandEvidence }) {
  return (
    <li className={`evidence-card is-${evidence.classification}`}>
      <div className="evidence-card-topline">
        <span className="evidence-classification">
          {evidence.classification.replaceAll("_", " ")}
        </span>
        <span className="evidence-score">Relevance {Math.round(evidence.relevance_score)}</span>
      </div>
      <h4>{evidence.title}</h4>
      <p className="evidence-domain">
        {evidence.domain}
        {evidence.published_at ? (
          <>
            <span aria-hidden="true"> · </span>
            <time dateTime={evidence.published_at}>
              {formatPublished(evidence.published_at)}
            </time>
          </>
        ) : null}
      </p>
      <p className="evidence-snippet">{evidence.snippet}</p>
      <p className="evidence-reason">{evidence.reason}</p>
      <Provenance queries={evidence.provenance.found_by_queries} />
      <a href={evidence.url} target="_blank" rel="noreferrer">
        View source ↗
      </a>
    </li>
  );
}

export function DemandEvidenceSection({
  collection,
  partial,
}: {
  collection: DemandEvidenceCollection;
  partial: boolean;
}) {
  const visibleCount = GROUPS.reduce(
    (total, group) =>
      total +
      collection.evidence.filter(
        (evidence) => evidence.classification === group.classification,
      ).length,
    0,
  );

  return (
    <section className="investigation-result-section evidence-section" aria-labelledby="demand-evidence-title">
      <header className="investigation-result-heading">
        <div>
          <p className="investigation-result-eyebrow">Market signals</p>
          <h2 id="demand-evidence-title">Demand evidence</h2>
        </div>
      </header>

      {partial ? (
        <p className="investigation-result-warning">
          Demand discovery was partial. The evidence below remains usable and
          comes from the searches that succeeded.
        </p>
      ) : null}

      {visibleCount === 0 ? (
        <div className="investigation-result-empty">
          <h3>
            No supporting or contradicting demand evidence was accepted from
            this discovery pass.
          </h3>
        </div>
      ) : null}

      {GROUPS.map((group) => {
        const evidence = collection.evidence.filter(
          (item) => item.classification === group.classification,
        );
        if (evidence.length === 0) return null;

        return (
          <section
            key={group.classification}
            className={`evidence-group is-${group.classification}`}
            aria-labelledby={`demand-${group.classification}-title`}
          >
            <header>
              <div>
                <h3 id={`demand-${group.classification}-title`}>{group.label}</h3>
                <p>{group.description}</p>
              </div>
              <span>{evidence.length}</span>
            </header>
            <ul className="evidence-card-list">
              {evidence.map((item) => (
                <EvidenceCard key={item.id} evidence={item} />
              ))}
            </ul>
          </section>
        );
      })}
    </section>
  );
}
