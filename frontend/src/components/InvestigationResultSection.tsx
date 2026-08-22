import { useState } from "react";
import type { ReactNode } from "react";
import "./InvestigationResultSection.css";

/**
 * The shared shell for the three terminal result surfaces (research,
 * demand evidence, competitor candidates). Collapsed by default -- the
 * eyebrow + title stay visible so the "Jump to surface" links at the top
 * of the page always land somewhere legible, but the body -- metrics,
 * cards, everything below -- only mounts once the user opens it.
 */
export function InvestigationResultSection({
  id,
  eyebrow,
  title,
  extraClassName,
  defaultOpen = false,
  children,
}: {
  /** Anchor id, matched by the "Jump to surface" links elsewhere on the page. */
  id: string;
  eyebrow: string;
  title: string;
  extraClassName?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = `${id}-content`;

  return (
    <section
      className={
        extraClassName
          ? `investigation-result-section ${extraClassName}`
          : "investigation-result-section"
      }
      aria-labelledby={id}
    >
      <header className="investigation-result-heading">
        <div>
          <p className="investigation-result-eyebrow">{eyebrow}</p>
          <h2 id={id}>{title}</h2>
        </div>
        <button
          type="button"
          className="investigation-result-toggle"
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Hide" : "Show"}
          <span className="investigation-result-chevron" aria-hidden="true" />
        </button>
      </header>

      {open && (
        <div id={contentId} className="investigation-result-body">
          {children}
        </div>
      )}
    </section>
  );
}
