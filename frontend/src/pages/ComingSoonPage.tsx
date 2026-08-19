import { PageShell } from "../components/PageShell";
import "./ComingSoonPage.css";

export function ComingSoonPage({
  title,
  subtitle,
}: {
  title: string;
  subtitle: string;
}) {
  return (
    <PageShell>
      <div className="container">
        <div className="coming-soon">
          <h1>{title}</h1>
          <p className="coming-soon-subtitle">{subtitle}</p>
          <div className="coming-soon-badge">Coming soon</div>
        </div>
      </div>
    </PageShell>
  );
}
