import "./EmptyState.css";

export function EmptyState({ onReset }: { onReset: () => void }) {
  return (
    <div className="empty-state">
      <p className="empty-state-title">No problems match your filters</p>
      <p className="empty-state-subtitle">
        Try a different keyword or clear your filters to see everything.
      </p>
      <button type="button" className="empty-state-reset" onClick={onReset}>
        Clear filters
      </button>
    </div>
  );
}
