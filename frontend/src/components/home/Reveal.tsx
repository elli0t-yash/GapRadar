import type { CSSProperties, ReactNode } from "react";
import { useInView } from "../../hooks/useInView";

/**
 * Fade-and-lift on first entry. Always a plain <div>: it is a layout
 * wrapper, so it must never change the semantics of what it wraps -- any
 * element that needs to be an <li>, a <section> or a heading owns its own
 * reveal via useInView instead.
 *
 * The motion itself lives entirely in CSS (see HomePage.css), so
 * `prefers-reduced-motion: reduce` cancels it without this component
 * knowing: the content is mounted, in the accessibility tree, and fully
 * opaque either way.
 */
export function Reveal({
  children,
  className,
  delay = 0,
}: {
  children: ReactNode;
  className?: string;
  /** Seconds of stagger inside one group. Keep small: 0 – 0.24s. */
  delay?: number;
}) {
  const [ref, inView] = useInView<HTMLDivElement>();

  return (
    <div
      ref={ref}
      className={`home-reveal${inView ? " is-in" : ""}${className ? ` ${className}` : ""}`}
      style={{ "--reveal-delay": `${delay}s` } as CSSProperties}
    >
      {children}
    </div>
  );
}
