import { Reveal } from "./Reveal";
import type { OpportunityFeed } from "../../hooks/useHomeData";

/**
 * Three surfaces over one core.
 *
 * The MCP tool count and the CLI command are facts about this repository
 * (17 registered tools; `gapradar opportunities list` is a real command),
 * and the rows under the CLI prompt are the live feed's actual top-ranked
 * titles rendered in a terminal style -- not invented output.
 */
const MCP_TOOL_COUNT = 17;

function truncate(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit - 1).trimEnd()}…`;
}

export function InterfacesShowcase({ feed }: { feed: OpportunityFeed | null }) {
  const top = feed?.problems.slice(0, 3) ?? [];

  return (
    <section
      className="home-section home-interfaces"
      data-surface="dark"
      aria-labelledby="home-interfaces-title"
    >
      <div className="container">
        <Reveal className="home-section-head">
          <p className="home-eyebrow">Interfaces</p>
          <h2 id="home-interfaces-title" className="home-section-title">
            GapRadar isn't trapped
            <span> inside a dashboard.</span>
          </h2>
          <p className="home-lede">
            Web, MCP, and CLI are different interfaces over the same GapRadar
            application services — the same database, the same scoring, the
            same reliability gate.
          </p>
        </Reveal>

        <div className="interfaces-grid">
          <Reveal className="interface-panel is-web">
            <p className="interface-kicker">Web</p>
            <div className="interface-window" aria-hidden="true">
              <span className="interface-window-bar" />
              <div className="interface-window-body">
                {top[0] ? (
                  <>
                    <span className="interface-tag">
                      {top[0].category ?? "Uncategorised"}
                    </span>
                    <p className="interface-window-title">
                      {truncate(top[0].title, 70)}
                    </p>
                    <span className="interface-window-score">
                      {top[0].signal === null ? "Not scorable" : top[0].signal}
                    </span>
                  </>
                ) : (
                  <>
                    <span className="interface-skeleton" />
                    <span className="interface-skeleton is-short" />
                  </>
                )}
              </div>
            </div>
            <p className="interface-copy">
              The ranked feed, opportunity detail, evidence workspaces, and the
              reliability record.
            </p>
          </Reveal>

          <Reveal className="interface-panel is-mcp" delay={0.06}>
            <p className="interface-kicker">MCP</p>
            <div className="interface-console">
              <p className="interface-console-prompt">
                <span aria-hidden="true">&gt;</span> What are GapRadar's
                strongest opportunities?
              </p>
              <p className="interface-console-answer">
                <strong>GapRadar MCP</strong>
                <span>{MCP_TOOL_COUNT} tools</span>
              </p>
              <p className="interface-console-note">
                Read tools answer directly; anything that could spend provider
                budget stays behind an explicit action.
              </p>
            </div>
            <p className="interface-copy">
              An agent in Codex or Claude queries the same services this page
              reads.
            </p>
          </Reveal>

          <Reveal className="interface-panel is-cli" delay={0.12}>
            <p className="interface-kicker">CLI</p>
            <div className="interface-console">
              <p className="interface-console-prompt">
                <span aria-hidden="true">$</span> gapradar opportunities list
              </p>
              <ol className="interface-console-rows">
                {top.length > 0
                  ? top.map((problem, index) => (
                      <li key={problem.id}>
                        <span>{String(index + 1).padStart(2, "0")}</span>
                        <span>{truncate(problem.title, 42)}</span>
                        <span>
                          {problem.signal === null ? "—" : problem.signal}
                        </span>
                      </li>
                    ))
                  : [0, 1, 2].map((index) => (
                      <li key={index} aria-hidden="true">
                        <span>{String(index + 1).padStart(2, "0")}</span>
                        <span className="interface-skeleton" />
                        <span>—</span>
                      </li>
                    ))}
              </ol>
              <p className="interface-console-note">
                Rows are the live feed's current top three, rendered here in the
                CLI's shape.
              </p>
            </div>
            <p className="interface-copy">
              The terminal path for developers, with the same ranking the web
              app shows.
            </p>
          </Reveal>
        </div>

        <Reveal className="interfaces-core" delay={0.06}>
          <ul className="interfaces-core-legs" aria-hidden="true">
            <li>Web</li>
            <li>MCP</li>
            <li>CLI</li>
          </ul>
          <span className="interfaces-core-lines" aria-hidden="true" />
          <p className="interfaces-core-label">One intelligence core</p>
          <p className="interfaces-core-copy">
            No interface has its own copy of the intelligence. There is one set
            of application services, and three ways to ask it a question.
          </p>
        </Reveal>
      </div>
    </section>
  );
}
