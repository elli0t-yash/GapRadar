import { useEffect, useLayoutEffect, useRef, useState } from "react";
import "./DockingHeading.css";

// ---------------------------------------------------------------------------
// Tunable timings (ms unless noted).
// ---------------------------------------------------------------------------
const HOLD_MS = 900; // how long the heading stays centered alone before docking
const DOCK_TRANSITION_MS = 750; // duration of the center-to-top animation
const DOCK_EASING = "cubic-bezier(.2,.85,.25,1)";

type Phase = "centered" | "docking" | "docked";

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

export function DockingHeading({
  ready = true,
  onDockComplete,
}: {
  ready?: boolean;
  onDockComplete: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("centered");
  const wrapperRef = useRef<HTMLDivElement>(null);
  const firstRectRef = useRef<DOMRect | null>(null);

  // Reduced motion: skip straight to the docked layout, no animation, as
  // soon as we're allowed to start (e.g. once a preceding intro finishes).
  useEffect(() => {
    if (!ready || !prefersReducedMotion()) return;
    setPhase("docked");
    onDockComplete();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  // Once ready, hold centered for a beat, then capture that position (FLIP
  // "first") before switching the wrapper to its docked (in-flow) layout.
  useEffect(() => {
    if (!ready || phase !== "centered" || prefersReducedMotion()) return;
    const t = window.setTimeout(() => {
      const el = wrapperRef.current;
      if (el) firstRectRef.current = el.getBoundingClientRect();
      setPhase("docking");
    }, HOLD_MS);
    return () => window.clearTimeout(t);
  }, [ready, phase]);

  // Invert to the old (centered) position instantly, then play the
  // transition back to the natural (docked) position.
  useLayoutEffect(() => {
    if (phase !== "docking") return;
    const el = wrapperRef.current;
    const first = firstRectRef.current;
    if (!el || !first) return;

    const last = el.getBoundingClientRect();
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    el.style.transition = "none";
    el.style.transform = `translate(${dx}px, ${dy}px)`;

    void el.offsetHeight; // force reflow before re-enabling the transition

    const raf = requestAnimationFrame(() => {
      el.style.transition = `transform ${DOCK_TRANSITION_MS}ms ${DOCK_EASING}`;
      el.style.transform = "translate(0, 0)";
    });

    const t = window.setTimeout(() => {
      setPhase("docked");
      onDockComplete();
    }, DOCK_TRANSITION_MS);

    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  const isCentered = phase === "centered";

  return (
    <div
      ref={wrapperRef}
      className={`docking-heading${isCentered ? " is-centered" : " is-docked"}`}
    >
      <h1 className="docking-heading-title">
        <span className="docking-heading-line">Scraped the web.</span>
        <span className="docking-heading-line">
          Kept only <em className="docking-heading-accent">the best.</em>
        </span>
      </h1>
      <p className="docking-heading-subtitle">
        We crawl real problem reports from across the web and surface only
        the sharpest opportunities.
      </p>
    </div>
  );
}
