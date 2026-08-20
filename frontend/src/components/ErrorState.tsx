import "./ErrorState.css";

/**
 * A failure scoped to one section. Never blanks the page: the rest of the
 * app keeps working while this panel offers a retry.
 */
export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
  note,
}: {
  title?: string;
  message: string;
  onRetry: () => void;
  note?: string;
}) {
  return (
    <div className="error-state" role="alert">
      <span className="error-state-mark" aria-hidden="true">!</span>
      <p className="error-state-title">{title}</p>
      <p className="error-state-message">{message}</p>
      {note ? <p className="error-state-note">{note}</p> : null}
      <button type="button" className="error-state-retry" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}
