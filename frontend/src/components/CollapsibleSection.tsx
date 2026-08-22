import { useId, useState } from "react";
import type { ReactNode } from "react";

/**
 * A dropdown wrapper for one `problem-detail-section`. Collapsed by
 * default -- the section header stays visible and its body only mounts
 * once the user opens it, so a detail panel with several of these never
 * dumps every section's content on screen at once.
 */
export function CollapsibleSection({
  label,
  title,
  defaultOpen = false,
  children,
}: {
  /** Small eyebrow above the title, e.g. "Source trail". */
  label?: string;
  title: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const reactId = useId();
  const contentId = `${reactId}-content`;

  return (
    <section className="problem-detail-section is-collapsible">
      {label && <p className="problem-detail-section-label">{label}</p>}
      <h3>
        <button
          type="button"
          className="problem-detail-section-toggle"
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen((o) => !o)}
        >
          <span>{title}</span>
          <span className="problem-detail-section-chevron" aria-hidden="true" />
        </button>
      </h3>
      {open && (
        <div id={contentId} className="problem-detail-section-content">
          {children}
        </div>
      )}
    </section>
  );
}
