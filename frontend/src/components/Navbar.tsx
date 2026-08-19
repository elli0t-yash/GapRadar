import { NavLink } from "react-router-dom";
import "./Navbar.css";

const NAV_ITEMS = [
  { to: "/trends", label: "Trends" },
  { to: "/saved", label: "Saved" },
];

export function Navbar() {
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
                    isActive ? "navbar-link is-active" : "navbar-link"
                  }
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <a
          className="navbar-cta"
          href="https://razorpay.com/m/fix-my-itch/"
          target="_blank"
          rel="noreferrer"
        >
          Submit a problem
        </a>
      </div>
    </header>
  );
}
