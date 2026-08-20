/**
 * Theme state for the app.
 *
 * Resolution order, matching the pre-hydration script in index.html:
 *   1. an explicit choice the user made before, from localStorage
 *   2. otherwise the operating system's preference
 *
 * Once the user toggles, the choice is explicit and the system no longer
 * overrides it. Until then the app follows the system live.
 */

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "gapradar-theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

/** Private-mode Safari throws on storage access; a theme is never worth it. */
function readStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* preference simply will not persist */
  }
}

function systemTheme(): Theme {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(DARK_QUERY).matches
    ? "dark"
    : "light";
}

function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", theme);
}

/** null means "no explicit choice yet, follow the system". */
let explicit: Theme | null = readStoredTheme();
let current: Theme = explicit ?? systemTheme();

const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function setResolved(theme: Theme): void {
  if (theme === current) return;
  current = theme;
  applyTheme(theme);
  emit();
}

export function getTheme(): Theme {
  return current;
}

export function setTheme(theme: Theme): void {
  explicit = theme;
  writeStoredTheme(theme);
  setResolved(theme);
}

export function toggleTheme(): void {
  setTheme(current === "dark" ? "light" : "dark");
}

export function subscribeToTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/* Re-assert on load: the inline script already stamped the attribute, this
   just keeps the two in step if storage changed between them. */
applyTheme(current);

if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
  window
    .matchMedia(DARK_QUERY)
    .addEventListener("change", (event: MediaQueryListEvent) => {
      if (explicit !== null) return;
      setResolved(event.matches ? "dark" : "light");
    });

  /* Another tab toggled the theme -- follow it rather than drift apart. */
  window.addEventListener("storage", (event: StorageEvent) => {
    if (event.key !== THEME_STORAGE_KEY) return;
    if (isTheme(event.newValue)) {
      explicit = event.newValue;
      setResolved(event.newValue);
    } else {
      explicit = null;
      setResolved(systemTheme());
    }
  });
}
