import { NavLink, useLocation } from "react-router-dom";
import { ThemeToggle } from "./ThemeToggle";
import "./Navbar.css";

const NAV_ITEMS = [
  { to: "/investigate", label: "Investigate" },
  { to: "/trends", label: "Trends" },
  { to: "/reliability", label: "Reliability" },
  { to: "/saved", label: "Saved" },
];

export function Navbar() {
  const location = useLocation();

  return (
    <header className="navbar">
      <div className="navbar-inner">
        <NavLink to="/" className="navbar-brand">
          GapRadar
        </NavLink>

        <nav className="navbar-nav" aria-label="Primary">
          <ul>
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  className={({ isActive }) =>
                    isActive ||
                    (item.to === "/investigate" &&
                      location.pathname.startsWith("/investigations/"))
                      ? "navbar-link is-active"
                      : "navbar-link"
                  }
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="navbar-actions">
          <ThemeToggle />
          <a
            className="navbar-cta"
            href="https://razorpay.com/m/fix-my-itch/"
            target="_blank"
            rel="noreferrer"
          >
            Submit a problem
          </a>
        </div>
      </div>
    </header>
  );
}
