import type { ReactNode } from "react";
import { Navbar } from "./Navbar";
import "./PageShell.css";

export function PageShell({ children }: { children: ReactNode }) {
  return (
    <div className="page-shell">
      <Navbar />
      <main className="page-shell-content">{children}</main>
    </div>
  );
}
