import "./ErrorState.css";

/**
 * A failure scoped to one section. Never blanks the page: the rest of the
 * app keeps working while this panel offers a retry.
 */
export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
}: {
  title?: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="error-state" role="alert">
      <p className="error-state-title">{title}</p>
      <p className="error-state-message">{message}</p>
      <button type="button" className="error-state-retry" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}
