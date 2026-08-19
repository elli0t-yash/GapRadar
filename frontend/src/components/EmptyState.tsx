import "./EmptyState.css";

export function EmptyState({
  title = "No problems match your filters",
  subtitle = "Try a different keyword or clear your filters to see everything.",
  onReset,
}: {
  title?: string;
  subtitle?: string;
  onReset: () => void;
}) {
  return (
    <div className="empty-state">
      <p className="empty-state-title">{title}</p>
      <p className="empty-state-subtitle">{subtitle}</p>
      <button type="button" className="empty-state-reset" onClick={onReset}>
        Clear filters
      </button>
    </div>
  );
}
