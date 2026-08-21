import "./HomeRadar.css";

/**
 * Decorative signal positions.
 *
 * These coordinates mean NOTHING. They are not geography, not a measured
 * bearing, and not a signal's position in any space the product models --
 * they exist so the scope reads as a radar. The only real numbers in this
 * component are the ones in the readout below it, and those come from the
 * live feed. The caption says so on screen.
 */
const DECORATIVE_BLIPS = [
  { cx: 118, cy: 74, r: 3.4, delay: "0s" },
  { cx: 71, cy: 118, r: 2.6, delay: "1.8s" },
  { cx: 132, cy: 133, r: 2.2, delay: "3.4s" },
  { cx: 92, cy: 58, r: 2, delay: "5.1s" },
  { cx: 144, cy: 101, r: 2.4, delay: "6.6s" },
];

function readout(value: number | null): string {
  return value === null ? "—" : String(value);
}

export function HomeRadar({
  opportunityCount,
  marketCount,
}: {
  /** Opportunities the feed actually returned. Null while loading/failed. */
  opportunityCount: number | null;
  /** Distinct industries present in that feed. */
  marketCount: number | null;
}) {
  return (
    <div className="home-radar">
      <div className="home-radar-scope">
        <svg viewBox="0 0 200 200" aria-hidden="true" focusable="false">
          <circle className="home-radar-ring" cx="100" cy="100" r="88" />
          <circle className="home-radar-ring" cx="100" cy="100" r="72" />
          <circle className="home-radar-ring" cx="100" cy="100" r="47" />
          <circle className="home-radar-ring" cx="100" cy="100" r="22" />
          <line className="home-radar-axis" x1="100" y1="12" x2="100" y2="188" />
          <line className="home-radar-axis" x1="12" y1="100" x2="188" y2="100" />

          {DECORATIVE_BLIPS.map((blip) => (
            <circle
              key={`${blip.cx}-${blip.cy}`}
              className="home-radar-blip"
              cx={blip.cx}
              cy={blip.cy}
              r={blip.r}
              style={{ animationDelay: blip.delay }}
            />
          ))}
        </svg>

        <span className="home-radar-sweep" aria-hidden="true" />
        <span className="home-radar-hub" aria-hidden="true">
          GR
        </span>
      </div>

      <dl className="home-radar-readout">
        <div>
          <dt>Opportunities in the feed</dt>
          <dd>{readout(opportunityCount)}</dd>
        </div>
        <div>
          <dt>Markets represented</dt>
          <dd>{readout(marketCount)}</dd>
        </div>
        <div className="is-policy">
          <dt>Source policy</dt>
          <dd>RecallGuard gated</dd>
        </div>
      </dl>

      <p className="home-radar-note">
        The scope is an illustration. Blip positions are decorative and encode
        no location, bearing, or measurement — the two counts above are read
        from the live opportunity feed.
      </p>
    </div>
  );
}
