# GapRadar Demo Runbook

## Core positioning
**GapRadar is a radar for market gaps — not just another web scraper.** It turns live web signals into evidence-backed opportunities, combines them with emerging research and competitive intelligence, and uses a trust layer to stop broken extraction from influencing decisions.

**One-line pitch:**  
A trust-aware opportunity intelligence platform that discovers market gaps and investigates whether they are real.

**Best contrast line:**  
Most scraping products answer: “What does the web say?”  
GapRadar answers: “What opportunity is hiding inside what the web is saying — and can I trust the evidence?”

---

# 1. Demo order

Show GapRadar in this order:

1. Home / Opportunity Dashboard
2. Top Opportunity
3. Research behind the Opportunity
4. RecallGuard / Reliability
5. Independent Investigation
6. Investigation Progress
7. Research + Demand + Competitors
8. MCP through Codex
9. CLI
10. Architecture / Closing Pitch

Do not start with MCP or CLI. First prove the product value, then show the other interfaces.

---

# 2. Opening — 20–30 seconds

## Show
Open the main GapRadar dashboard.

## Say
“Web scraping itself is easy. The harder problem is turning noisy web data into something useful and trustworthy.

GapRadar is a radar for market gaps. It detects real-world pain signals, turns them into ranked opportunities, connects those signals with emerging research, and protects the pipeline with a reliability layer so bad extraction does not silently become bad intelligence.”

Then explain:

- **Discovery Mode** — automatically finds opportunities from live web signals.
- **Investigation Mode** — lets us give GapRadar any idea and independently investigate the evidence around it.

---

# 3. Opportunity Dashboard — Discovery Mode

## Show
- Opportunity list
- Ranking / score
- Industry/category
- Problem title
- Supporting evidence summary

## Say
“These are not raw scraped pages. GapRadar converts trusted market signals into opportunity candidates and ranks them using the opportunity engine.”

“Instead of showing 1,000 scraped records, we surface the market gaps hidden inside them.”

Use words such as:
- evidence-backed
- trust-gated
- traceable
- ranked opportunity

Avoid saying “fully proven.”

---

# 4. Open the strongest Opportunity

## Show
Open one of the highest-ranked opportunities.

Show:
- Problem / opportunity
- Score
- Source/evidence context
- Research section

## Say
“Here GapRadar moves from discovery into explanation. We can see what problem was detected, why it ranked highly, and what technical or research evidence exists around it.”

---

# 5. Show Academic Research

## Show
- Relevant papers
- Relevance score
- Matched concepts
- Research reasoning
- Technical readiness where available
- Paper links

## Say
“GapRadar does not only ask whether people have a problem. It also asks whether technology is becoming capable of solving it.

The research layer searches academic work, semantically judges relevance, and stores the results that cross our relevance threshold.”

Say “Relevant academic research,” not “science proves this startup will work.”

---

# 6. Show RecallGuard / Reliability

## Show
- Reliability overview
- Extraction incident
- Drift detection
- Before/after health if available
- Repair proof / fixture replay
- Historical Bright Data evidence

## Say
“This is where GapRadar differs from a normal intelligence dashboard.

If a website changes and the scraper starts extracting the wrong field, a normal pipeline can keep producing confident-looking garbage.

RecallGuard monitors extraction quality, identifies drift, classifies the failure, and prevents unreliable data from silently entering opportunity decisions.”

Important wording:

“We have deterministic self-healing proof through our controlled RecallGuard fixture workflow. With live Bright Data data, we have proven real extraction-drift detection and safety rejection.”

Do **not** claim full autonomous live-site self-healing in production.

Good transition:

“GapRadar does not only ask ‘what did we scrape?’ — it also asks ‘can we trust what we scraped?’”

---

# 7. Independent Investigation — Main wow moment

## Show
Open `/investigate`.

## Say
“Discovery Mode finds opportunities automatically. But what if I already have an idea?

Investigation Mode lets me give GapRadar any hypothesis, even if it was never discovered by our existing opportunity pipeline.”

## Example
**Query:** AI compliance assistant for small Indian exporters  
**Industry:** B2B SaaS / Export compliance

Alternative:
AI inventory forecasting for independent restaurants

## Step
Create the Investigation.

## Say
“Creating the Investigation is intentionally separate from running it. We persist the hypothesis first, and no provider budget is spent until the user explicitly chooses Analyse.”

---

# 8. Click Analyse Idea

## Show
Click **Analyse Idea** exactly once.

## Say
“Now GapRadar independently builds three evidence streams around the idea.”

### Academic Research
“Is the technology technically feasible or emerging?”

### Demand Evidence
“Are people or businesses actually expressing this problem?”

### Competitor Intelligence
“Who is already solving it, and what type of competition exists?”

---

# 9. Show Investigation Progress

## Show
- Planning
- Research queries
- Papers discovered
- Papers judged/matched
- Demand searches
- Demand candidates
- Competitor searches
- Competitor candidates

## Say
“We do not show fake progress bars or invented percentages. Every number here comes from persisted backend phase counters.”

If a phase is partial:

“Partial does not mean useless. GapRadar preserves whatever evidence succeeded instead of throwing away the whole investigation.”

---

# 10. Show Investigation Research

## Show
- Papers
- Concepts
- Match reason
- Relevance
- Readiness if available

## Say
“This gives us the technical side of the hypothesis.”

---

# 11. Show Demand Evidence

## Show
- STRONG_SUPPORT
- SUPPORT
- CONTRADICTS
- Source domain
- Snippet
- Reason
- Provenance
- “Found across N search directions” when available

## Say
“GapRadar keeps contradictory evidence visible. We do not hide evidence just because it weakens the idea. If multiple independent search directions discover the same source, we preserve that convergence.”

Say “supporting and contradicting demand evidence,” not “demand is proven.”

---

# 12. Show Competitor Candidates

## Show
- DIRECT
- ADJACENT
- SUBSTITUTE
- Domain
- Evidence
- Classification reason

## Say
“This is discovery-stage competitive intelligence. GapRadar separates direct competitors from adjacent and substitute solutions.”

Say “Competitor candidates,” not “Complete competitive landscape.”

Do not make pricing, funding, feature, or market-share claims unless separately verified.

---

# 13. Explain Bright Data Usage

## Say
“Bright Data provides the live web intelligence layer.

For Investigation discovery, one search query maps to one Bright Data SERP request. We normalize the organic results and classify the evidence without opening every result page.

That keeps the pipeline bounded instead of creating an uncontrolled N+1 scraping explosion.”

Mention:
- SERP API
- bounded query count
- organic results
- normalized URL/domain/snippet/rank
- no fabricated publication dates
- no mass result-page crawling in discovery

---

# 14. MCP Demo — Codex

Only show MCP after the web product is understood.

## Show
Open Codex connected to production GapRadar MCP.

## Prompts
1. “Use GapRadar MCP and show me the strongest opportunities currently stored.”
2. “Pick the strongest opportunity and explain its persisted academic research.”
3. “Check GapRadar’s reliability evidence and tell me whether there are extraction incidents I should know about.”
4. “Create an investigation for an AI compliance assistant for small Indian exporters. Do not run it yet.”

Optional:
“Run that investigation.”

## Say
“The same GapRadar intelligence is available to external AI agents through MCP.

This is not a separate demo database or wrapper. MCP calls the same GapRadar application services.”

Mention:
- 17 MCP tools
- authenticated Streamable HTTP
- read tools are provider-free
- `run_investigation` is explicitly provider-spending
- duplicate active runs are protected

---

# 15. CLI Demo

## Show
```bash
gapradar overview
gapradar opportunities list --limit 5
gapradar reliability overview
gapradar investigations list --limit 5
```

Optional:

```bash
gapradar investigations create   "AI compliance assistant for small Indian exporters"   --industry "B2B SaaS / Export compliance"
```

## Say
“The CLI is an MCP client, not a second implementation. The browser, MCP agents, and CLI all operate on the same GapRadar intelligence.”

Optional JSON mode:

```bash
gapradar overview --json
```

“This also makes GapRadar scriptable and automation-friendly.”

---

# 16. Architecture Explanation

```text
                         GapRadar
                            |
        +-------------------+-------------------+
        |                   |                   |
      Web UI               MCP                CLI
        |                   |                   |
      REST             MCP Tools           MCP Client
        |                   |                   |
        +-------------------+-------------------+
                            |
                  Application Services
                            |
        +-------------------+--------------------+
        |                   |                    |
  Opportunity Engine   Investigations       Reliability
        |                   |                    |
  Market Signals      Research/Demand       RecallGuard
                      /Competitors
        |
     Bright Data
```

## Say
“GapRadar is not built as three separate products. Web, MCP, and CLI are interfaces over the same application services.”

---

# 17. Technical Points to Mention if Asked

## Opportunity Engine
- Trusted persisted signal projection
- Existing opportunity scoring/ranking
- Research attached after semantic evaluation

## Investigation
- User-supplied hypothesis is not converted into a fake market Signal
- Separate Investigation domain
- One active run protected at database level
- Reload attaches to existing run
- Backend owns `is_terminal` and `is_retryable`
- Partial evidence is preserved

## Research
- Deterministic query planning first
- LLM fallback when needed
- arXiv discovery optimized to avoid detail-page fan-out
- Semantic relevance judgment
- Persisted evidence

## Bright Data
- SERP discovery
- One request per query
- Bounded query families
- Organic result normalization
- No indiscriminate page crawling

## RecallGuard
- Drift detection
- Classification
- Incident/proof model
- Controlled repair validation
- Honest distinction between fixture self-heal proof and real-provider evidence

## MCP
- Official MCP Python SDK
- Authenticated Streamable HTTP
- 17 tools
- Same application services as REST
- Clear read/write distinction

## CLI
- MCP-backed
- JSON mode
- Watch mode
- Confirmation before provider-spending run
- No automatic retry of ambiguous writes

---

# 18. Things NOT to Overclaim

Avoid:
- “GapRadar proves market demand.”
- “GapRadar guarantees startup success.”
- “This is a complete competitive landscape.”
- “RecallGuard has proven autonomous live self-healing on every source.”
- “Every scraped field is verified.”
- “The opportunity score predicts company success.”
- “The AI decides whether you should build the startup.”

Prefer:
- evidence-backed
- demand evidence
- competitor candidates
- relevant research
- trust-aware
- provenance-aware
- potential market gap
- opportunity candidate

---

# 19. Fallback Plan if a Provider is Slow

Never pretend fixture data is a live run.

If a live Investigation is slow:

1. Say: “The provider-backed investigation is still running.”
2. Open a previously completed persisted Investigation.
3. Show research, demand, competitors, and provenance.
4. If showing RecallGuard fixture replay, explicitly say:
   “This is our deterministic RecallGuard self-healing demonstration.”
5. Show historical live Bright Data reliability evidence separately.

Always keep ready:
- one completed Investigation
- one strong Opportunity
- one RecallGuard incident/proof
- Codex MCP already authenticated
- CLI environment configured

---

# 20. Pre-Demo Checklist

- [ ] Production frontend opens
- [ ] Production backend health = 200
- [ ] Opportunities load
- [ ] Opportunity research loads
- [ ] Reliability page loads
- [ ] `/investigate` loads
- [ ] One completed Investigation exists
- [ ] Bright Data key works
- [ ] OpenAI key works
- [ ] MCP production endpoint works
- [ ] Codex shows GapRadar MCP tools
- [ ] CLI can run `gapradar overview`
- [ ] Dark mode works if being shown
- [ ] Railway logs open in another tab
- [ ] No secrets visible on screen
- [ ] Backup screenshots/video available

---

# 21. Recommended 5-Minute Demo

## 0:00–0:30
Problem + positioning

“Scraping tells you what is on the web. GapRadar tries to identify what the market is missing — and whether the evidence can be trusted.”

## 0:30–1:20
Discovery Mode
- Dashboard
- Top Opportunity
- Opportunity score
- Research

## 1:20–2:00
RecallGuard
- Reliability
- Drift
- Proof
- Trust story

## 2:00–3:20
Independent Investigation
- Enter arbitrary idea
- Analyse
- Progress
- Research
- Demand
- Competitors

## 3:20–4:10
MCP
- Ask Codex for strongest opportunities
- Ask reliability question
- Show external AI-agent access

## 4:10–4:35
CLI
- `gapradar overview`
- `gapradar opportunities list`

## 4:35–5:00
Close

“GapRadar is not a scraper dashboard. It is a trust-aware opportunity intelligence layer available through Web, MCP, and CLI.”

---

# 22. Strong Closing

> “The web already contains millions of weak signals about what people need. The problem is that those signals are noisy, fragmented, and often extracted unreliably. GapRadar turns them into traceable opportunity intelligence — then lets humans and AI agents investigate those opportunities through the web, MCP, or CLI.”

End with:

> **Find what the market is missing — and know why you believe it.**
