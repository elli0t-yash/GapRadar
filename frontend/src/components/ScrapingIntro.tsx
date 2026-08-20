import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
// Decorative content for the intro animation ONLY. These are not
// opportunities, are never navigable, and are gone within seconds. The
// product feed has exactly one source: the backend (src/api/opportunities.ts).
import listingsData from "../data/intro-animation-listings.json";
import type { Listing } from "../types";
import "./ScrapingIntro.css";

// ---------------------------------------------------------------------------
// Tunable timings (ms unless noted). Change these to re-pace the sequence.
// ---------------------------------------------------------------------------
const POP_STAGGER_MS = 30; // delay between each card's pop-in start
const POP_DURATION_MS = 420; // duration of a single card's pop-in
const BEAM_START_DELAY_MS = 200; // pause after last card pops before the beam sweeps
const BEAM_DURATION_MS = 1600; // scanning beam sweep duration
const SCORE_REVEAL_DELAY_MS = 250; // pause after beam finishes before badges fade in
const SCORE_REVEAL_DURATION_MS = 400; // badge fade-in duration
const FILTER_DELAY_MS = 900; // pause after scores are shown before rejecting cards
const REJECT_FADE_DURATION_MS = 500; // rejected card fade/desaturate/shrink duration
const ARRANGE_DELAY_MS = 150; // pause after rejects are gone before the row animation starts
const ARRANGE_DURATION_MS = 800; // kept cards flying into the centered row
const ARRANGE_EASING = "cubic-bezier(.2,.85,.25,1)";
const ARRANGE_SETTLE_SCALE = 1.05; // slight overshoot scale for kept cards in the row
const LABEL_DELAY_MS = 250; // pause after cards settle before the label fades in
const FADE_OUT_DELAY_MS = 1200; // pause after the label before the overlay fades out
const OVERLAY_FADE_DURATION_MS = 600; // overlay fade-to-page duration
const REDUCED_MOTION_HOLD_MS = 500; // brief static hold when motion is reduced

const KEEP_COUNT = 6;

type Phase =
  | "grid"
  | "beam"
  | "reveal"
  | "filtering"
  | "settled"
  | "arranging"
  | "label"
  | "fading"
  | "reduced"
  | "done";

const listings = listingsData as Listing[];

// Narrative shown while each phase plays, so the animation reads as an
// actual scrape-and-rank pass over real problem data rather than a
// decorative loader.
const CAPTIONS: Partial<Record<Phase, string>> = {
  grid: "Pulling in fresh problem reports…",
  beam: "Scanning each one for real signal…",
  reveal: "Scoring severity, demand & whitespace…",
  filtering: "Filtering out the weak signals…",
  settled: "Ranking the strongest opportunities…",
  arranging: "Ranking the strongest opportunities…",
  reduced: `${KEEP_COUNT} top matches found`,
};

function captionFor(phase: Phase): string {
  return CAPTIONS[phase] ?? `${KEEP_COUNT} top matches found`;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

export function ScrapingIntro({ onDone }: { onDone?: () => void }) {
  const [phase, setPhase] = useState<Phase>("grid");

  const cardRefs = useRef(new Map<string, HTMLDivElement>());
  const firstRectsRef = useRef(new Map<string, DOMRect>());

  const keptIds = useMemo(() => {
    return new Set(
      [...listings]
        .sort((a, b) => b.score - a.score)
        .slice(0, KEEP_COUNT)
        .map((l) => l.id),
    );
  }, []);

  const visibleListings = useMemo(() => {
    if (
      phase === "settled" ||
      phase === "arranging" ||
      phase === "label" ||
      phase === "fading"
    ) {
      return listings.filter((l) => keptIds.has(l.id));
    }
    if (phase === "reduced") {
      return listings.filter((l) => keptIds.has(l.id));
    }
    return listings;
  }, [phase, keptIds]);

  // Main timing chain: pop-in (CSS-driven) -> beam -> reveal -> filter -> settle.
  useEffect(() => {
    if (phase === "done") return;

    if (prefersReducedMotion()) {
      setPhase("reduced");
      const t = window.setTimeout(() => setPhase("done"), REDUCED_MOTION_HOLD_MS);
      return () => window.clearTimeout(t);
    }

    const timers: number[] = [];
    const schedule = (fn: () => void, delay: number) => {
      timers.push(window.setTimeout(fn, delay));
    };

    const totalPopMs = (listings.length - 1) * POP_STAGGER_MS + POP_DURATION_MS;
    const beamStart = totalPopMs + BEAM_START_DELAY_MS;
    const revealStart = beamStart + BEAM_DURATION_MS + SCORE_REVEAL_DELAY_MS;
    const filterStart = revealStart + SCORE_REVEAL_DURATION_MS + FILTER_DELAY_MS;
    const settleStart = filterStart + REJECT_FADE_DURATION_MS;

    schedule(() => setPhase("beam"), beamStart);
    schedule(() => setPhase("reveal"), revealStart);
    schedule(() => setPhase("filtering"), filterStart);
    schedule(() => setPhase("settled"), settleStart);

    return () => timers.forEach(window.clearTimeout);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // FLIP step 1: measure the kept cards' grid positions once rejects are gone,
  // then flip into the row layout on the next render.
  useEffect(() => {
    if (phase !== "settled") return;
    const rects = new Map<string, DOMRect>();
    cardRefs.current.forEach((el, id) => {
      rects.set(id, el.getBoundingClientRect());
    });
    firstRectsRef.current = rects;
    const t = window.setTimeout(() => setPhase("arranging"), ARRANGE_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  // FLIP step 2: invert to the old position instantly, then play the
  // transition back to the natural (row) position.
  useLayoutEffect(() => {
    if (phase !== "arranging") return;

    const firsts = firstRectsRef.current;
    cardRefs.current.forEach((el, id) => {
      const first = firsts.get(id);
      if (!first) return;
      const last = el.getBoundingClientRect();
      const dx = first.left - last.left;
      const dy = first.top - last.top;
      const scaleX = last.width ? first.width / last.width : 1;
      const scaleY = last.height ? first.height / last.height : 1;
      el.style.transition = "none";
      el.style.transform = `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY})`;
    });

    // Force a reflow so the instant transform above is committed before we
    // switch the transition back on for the animated part.
    void document.body.offsetHeight;

    const raf = requestAnimationFrame(() => {
      cardRefs.current.forEach((el) => {
        el.style.transition = `transform ${ARRANGE_DURATION_MS}ms ${ARRANGE_EASING}`;
        el.style.transform = `translate(0, 0) scale(${ARRANGE_SETTLE_SCALE})`;
      });
    });

    const t = window.setTimeout(
      () => setPhase("label"),
      ARRANGE_DURATION_MS + LABEL_DELAY_MS,
    );

    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(t);
    };
  }, [phase]);

  useEffect(() => {
    if (phase !== "label") return;
    const t = window.setTimeout(() => setPhase("fading"), FADE_OUT_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  useEffect(() => {
    if (phase !== "fading") return;
    const t = window.setTimeout(() => setPhase("done"), OVERLAY_FADE_DURATION_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  useEffect(() => {
    if (phase === "done") onDone?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  if (phase === "done") return null;

  const isRowLayout =
    phase === "arranging" ||
    phase === "label" ||
    phase === "fading" ||
    phase === "reduced";
  const isFading = phase === "fading";
  const isBeamActive = phase === "beam";
  const scoresVisible =
    phase === "reveal" ||
    phase === "filtering" ||
    phase === "settled" ||
    phase === "arranging" ||
    phase === "label" ||
    phase === "fading" ||
    phase === "reduced";
  const labelVisible = phase === "label" || phase === "fading" || phase === "reduced";
  const caption = captionFor(phase);

  return (
    <div
      className={`scraping-intro${isFading ? " is-fading" : ""}${
        phase === "reduced" ? " is-no-motion" : ""
      }`}
    >
      <p
        className={`scraping-intro-caption${labelVisible ? " is-hidden" : ""}`}
        aria-live="polite"
        key={caption}
      >
        {caption}
      </p>

      <div
        className={`scraping-intro-grid${isRowLayout ? " is-row" : ""}`}
        aria-hidden="true"
      >
        {phase !== "reduced" && (
          <div className={`scraping-intro-beam${isBeamActive ? " is-active" : ""}`} />
        )}

        {visibleListings.map((listing, i) => {
          const isKept = keptIds.has(listing.id);
          const isRejected = phase === "filtering" && !isKept;
          // Deliberately withheld until the FLIP move (arranging) has
          // finished: the highlight's box-shadow/border-color transition is
          // a paint cost, and firing it while the transform is still
          // animating competes with that animation for frame budget and
          // reads as jank. Waiting for "label" keeps the move
          // compositor-only, then layers the highlight in as its own step.
          const showKeptHighlight = isKept && labelVisible;

          return (
            <div
              key={listing.id}
              ref={(el) => {
                if (el) cardRefs.current.set(listing.id, el);
                else cardRefs.current.delete(listing.id);
              }}
              className={`listing-card${isKept ? " is-kept" : ""}${
                isRejected ? " is-rejected" : ""
              }${isRowLayout ? " is-settled" : ""}${
                showKeptHighlight ? " is-kept-highlight" : ""
              }`}
              style={
                phase === "grid"
                  ? { animationDelay: `${i * POP_STAGGER_MS}ms` }
                  : undefined
              }
            >
              <div className="listing-card-top">
                <span className="listing-card-tag">{listing.category}</span>
                <span
                  className={`listing-score-badge${scoresVisible ? " is-visible" : ""}${
                    isKept ? " is-kept-score" : ""
                  }`}
                >
                  ★ {listing.score.toFixed(1)}
                </span>
              </div>
              <p className="listing-card-title">{listing.title}</p>
            </div>
          );
        })}
      </div>

      <span className={`scraping-intro-label${labelVisible ? " is-visible" : ""}`}>
        {KEEP_COUNT} top matches found
      </span>
    </div>
  );
}
