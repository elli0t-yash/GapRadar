import "./EmptyState.css";

export function EmptyState({
  title = "No problems match your filters",
  subtitle = "Try a different keyword or clear your filters to see everything.",
  onReset,
  actionLabel = "Clear filters",
}: {
  title?: string;
  subtitle?: string;
  onReset?: () => void;
  actionLabel?: string;
}) {
  return (
    <div className="empty-state">
      <span className="empty-state-signal" aria-hidden="true"><span /></span>
      <p className="empty-state-title">{title}</p>
      <p className="empty-state-subtitle">{subtitle}</p>
      {onReset ? (
        <button type="button" className="empty-state-reset" onClick={onReset}>
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
}
