import { useEffect, useRef, useState } from "react";

/**
 * "Has this element reached the viewport at least once?"
 *
 * Latches on: the landing page uses it both for scroll reveals and to defer
 * the showcase fetches until their section is actually approaching, and
 * neither should undo itself when the user scrolls back up.
 *
 * Browsers without IntersectionObserver report `true` immediately, so the
 * page degrades to "everything visible, nothing animated" rather than to a
 * blank column.
 */
export function useInView<T extends Element>(
  rootMargin = "0px 0px -12% 0px",
): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T | null>(null);
  const [inView, setInView] = useState(
    () => typeof IntersectionObserver === "undefined",
  );

  useEffect(() => {
    if (inView) return;
    const element = ref.current;
    if (!element) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setInView(true);
          observer.disconnect();
        }
      },
      { rootMargin },
    );

    observer.observe(element);
    return () => observer.disconnect();
  }, [inView, rootMargin]);

  return [ref, inView];
}
