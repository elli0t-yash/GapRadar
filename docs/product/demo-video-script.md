# GapRadar — Demo Video Script (technical cut, under 4 minutes)

Target length: 3:30–3:50. Each section lists **[SCREEN]** (what to
show/do) and **SAY** (narration — technical, precise, no hand-waving).
Keep claims matched to what's actually implemented; the "Precision notes"
at the end list phrasing to avoid.

---

## 0. Cold open (0:00–0:15)

**[SCREEN]** GapRadar landing page, opportunity feed already loaded.

**SAY**
> "This is GapRadar — an opportunity-intelligence platform. It turns
> public web signals into ranked, evidence-backed market opportunities,
> and every signal has to survive a reliability gate before it can
> influence a ranking. Let's go straight through the pipeline."

---

## 1. Discovery pipeline (0:15–0:45)

**[SCREEN]** Scroll the opportunity feed; open one card; point at the
score.

**SAY**
> "Collectors run against Bright Data's Scraper Studio — Razorpay's 'Fix
> My Itch' board, plus an arXiv collector for research. Every record is
> validated against a strict Pydantic source contract before ingestion,
> and each opportunity is scored on four deterministic components —
> severity, TAM, whitespace, frequency — not an LLM guessing a number."

---

## 2. RecallGuard — the reliability layer (0:45–2:15)

*This is the standout feature. Give it the most time.*

**[SCREEN]** Navigate to the Reliability page; start the fixture demo
(`POST /reliability/demo/start`).

**SAY**
> "Here's what makes GapRadar trust-aware: RecallGuard. A collector run
> finishing `SUCCEEDED` is an execution fact, not a trust verdict — we had
> a real bug where a scraper returned `60` for a field the source shows on
> a 1-to-10 scale. The run succeeded; the data was wrong. Every run goes
> through five deterministic checks — execution, payload shape, source
> contract, ingestion, and completeness against an *observed baseline*, so
> growth always passes and only a real collapse fails."

**[SCREEN]** Advance the demo: incident opens `DEGRADED` /
`EXTRACTION_DRIFT` → healing → candidate rejected → healing again →
approved → verifying.

**SAY**
> "A failing check opens an incident, classified either `OUTAGE` — just
> needs a retry — or `EXTRACTION_DRIFT`, which triggers Bright Data's
> self-healing AI with a deterministic, evidence-built prompt. The repair
> candidate isn't trusted yet — its preview has to pass the same strict
> contract production data faces, or it's rejected automatically. Three
> autonomous attempts, then it escalates to a human."

**[SCREEN]** Advance to `RECOVERED`; show the recovery proof.

**SAY**
> "The rule that matters most: approval is not recovery. Only a brand-new,
> independent production run — passing every check again from scratch —
> can close the incident. That's this `RecoveryProof`: detection run,
> verification run, attempt number, every check. Nothing about a repair is
> ever taken on trust, and only currently-healthy collectors ever feed
> trusted data downstream."

---

## 3. Investigation mode (2:15–3:15)

**[SCREEN]** Navigate to `/investigate`, submit a hypothesis, start the
run; show phase progress.

**SAY**
> "Investigation mode lets a user submit their own hypothesis. Running it
> is an explicit, user-triggered action that drives four phases with real
> state — pending, running, complete, partial, failed, or skipped, never a
> synthesized percentage."

**[SCREEN]** Open "Relevant academic research," "Demand evidence,"
"Competitor candidates" in turn.

**SAY**
> "Three evidence streams come back: research papers from arXiv with
> relevance and technical-readiness scores; demand evidence from Bright
> Data's SERP API, classified support through contradicts — contradicting
> evidence is kept and shown, never filtered out; and competitors
> classified direct, adjacent, or substitute, because 'the competitor is a
> spreadsheet' is often the true answer."

---

## 4. Close — architecture + agent interfaces (3:15–3:45)

**[SCREEN]** Architecture diagram: Bright Data → ingestion/validation →
RecallGuard → opportunity/investigation services → PostgreSQL + FastAPI →
React / MCP / CLI.

**SAY**
> "End to end: Bright Data feeds a FastAPI backend on Postgres, every run
> passes through RecallGuard before it's trusted, and the same domain
> services back the React frontend plus an MCP server and CLI for agent
> use — same reliability guarantees either way. An AI system touching
> real-world data has to be able to prove its own reliability, not just
> claim it — that's RecallGuard, and it's what makes GapRadar different
> from a scraper with a dashboard bolted on. Code and live app are linked
> below."

---

## Precision notes (don't say these)

- Don't say "fully autonomous self-healing in production" without showing
  it against a *real* incident — the safe, always-true claim is "the
  fixture-replay demo runs the identical lifecycle functions a real
  incident uses," which is what `app/recallguard/demo.py` actually does.
- Don't claim the score predicts business outcomes — it's a structured,
  deterministic signal score, not a guarantee.
- Don't claim server-side search/filtering — filtering is client-side over
  one bounded, already-ranked fetch.
- Say "reliability-gated" / "evidence-backed," not "verified accurate" —
  RecallGuard proves *extraction* reliability, not that the underlying
  real-world fact is true.

## Timing cheat sheet

| Section | Time | Cumulative |
|---|---|---|
| Cold open | 0:15 | 0:15 |
| Discovery pipeline | 0:30 | 0:45 |
| RecallGuard (headline feature) | 1:30 | 2:15 |
| Investigation mode | 1:00 | 3:15 |
| Close (architecture + MCP/CLI) | 0:30 | 3:45 |

If you're still over: trim the investigation section to two evidence
streams instead of three, and cut the second RecallGuard beat (candidate
rejection) down to one clause — "a rejected repair still counts against
the three-attempt budget." Don't cut RecallGuard's headline beats
(baseline-based checks, and approval ≠ recovery) — that's the one story
that makes this project stand out from a plain scraper demo.
