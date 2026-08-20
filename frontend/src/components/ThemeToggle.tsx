import { useTheme } from "../hooks/useTheme";
import { toggleTheme } from "../theme";
import "./ThemeToggle.css";

/**
 * Compact light/dark switch for the navbar. Shows the theme you would move
 * to, which is what the label announces.
 */
export function ThemeToggle() {
  const theme = useTheme();
  const isDark = theme === "dark";
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggleTheme}
      aria-label={label}
      title={label}
    >
      {isDark ? (
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="4.2" />
          <g strokeLinecap="round">
            <path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2" />
            <path d="M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6" />
          </g>
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path
            d="M20.2 14.4A8.4 8.4 0 0 1 9.6 3.8a8.4 8.4 0 1 0 10.6 10.6Z"
            strokeLinejoin="round"
          />
        </svg>
      )}
    </button>
  );
}
