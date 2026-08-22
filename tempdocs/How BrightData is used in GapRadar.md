# GapRadar — How Bright Data Is Used

GapRadar is a **trust-aware opportunity intelligence platform** built around one core question:

> **What is the market missing?**

To answer that, GapRadar needs fresh evidence from the public web. Bright Data is the web-intelligence acquisition layer used across both major GapRadar workflows:

- **Discovery Mode** — finds and ranks market opportunities.
- **Investigation Mode** — investigates a user-supplied hypothesis using research, demand, and competitor evidence.

Bright Data is currently used in **three places** across the product.

---

## 1. Razorpay “Fix My Itch” → Discovering Market Problems

For **Discovery Mode**, GapRadar starts with Razorpay’s public **Fix My Itch** page:

**Source:** https://razorpay.com/m/fix-my-itch/

The page contains real-world business and consumer problems across multiple industries.

We built a **custom scraper in Bright Data Scraper Studio** that visits this page and converts the information into structured records that GapRadar can process.

For each problem, the scraper collects information such as:

- Problem statement
- Description
- Industry
- Itch Score
- Severity score
- TAM score
- Whitespace score
- Frequency score
- Original source URL

### Example

Instead of GapRadar receiving a large webpage, Bright Data returns a structured record such as:

```text
Problem:
Why do micro-SMEs waste 10+ hours weekly on invoice management?

Industry:
B2B Services

Severity:
7

Frequency:
9

Source:
Razorpay Fix My Itch
```

The GapRadar backend validates the structure of the Bright Data collector output before that data is allowed into the opportunity pipeline.

### Discovery Flow

```text
Razorpay Fix My Itch
        ↓
Bright Data Scraper Studio
        ↓
Structured market problems
        ↓
RecallGuard reliability checks
        ↓
GapRadar Opportunity Engine
        ↓
Ranked opportunities in Discovery Mode
```

This is how GapRadar gets its initial set of real-world market pain points.

---

## 2. arXiv → Finding Academic Research During an Investigation

GapRadar also lets users investigate their **own market hypothesis**.

For example:

> “Is there an opportunity for AI tools that automatically simplify compliance work for small businesses?”

GapRadar does not immediately generate a yes/no answer.

It first looks for evidence.

One of those evidence sources is **academic research from arXiv**.

GapRadar generates relevant research queries and converts them into arXiv search URLs.

### Research Flow

```text
User hypothesis
        ↓
GapRadar creates research queries
        ↓
arXiv search URL
        ↓
Bright Data arXiv collector
        ↓
Academic papers
        ↓
GapRadar research evidence
```

For each research query, GapRadar triggers the Bright Data arXiv collector, waits for the Bright Data job to complete, retrieves the resulting papers, and then processes them as research evidence.

The current production integration searches the first arXiv results page and collects up to **15 results per generated research query**.

This evidence appears in the **Research Evidence** section of an Investigation.

In simple terms, this part answers:

> **“Is there academic research related to this problem or technology?”**

---

## 3. Bright Data SERP API → Demand Evidence and Competitor Discovery

Academic research alone cannot tell us whether people actually want a solution.

Investigation Mode also needs evidence from the wider public web.

For this, GapRadar uses the **Bright Data SERP API**.

GapRadar generates Google search queries based on the user’s hypothesis and sends them through Bright Data’s SERP API.

Bright Data returns structured **organic Google search results**, so GapRadar does not need to scrape Google directly.

The implementation intentionally uses organic results rather than advertisements or knowledge-panel content.

The returned web evidence is used for two major purposes.

### A. Demand Evidence

GapRadar searches for signals that suggest people are:

- experiencing the problem,
- complaining about the current situation,
- searching for alternatives,
- asking for solutions,
- or discussing unmet needs.

For example, for the hypothesis:

> “Small businesses need easier compliance automation.”

GapRadar may search for evidence around:

- difficulty managing compliance manually,
- complaints about compliance workload,
- demand for compliance automation,
- discussions about existing solutions,
- and recurring pain points.

### Demand Flow

```text
User hypothesis
        ↓
GapRadar generates demand-focused searches
        ↓
Google Search
        ↓
Bright Data SERP API
        ↓
Structured organic web results
        ↓
GapRadar classifies useful evidence
        ↓
Demand Evidence
```

This helps answer:

> **“Are people actually experiencing or searching for a solution to this problem?”**

---

### B. Competitor Candidates

The same Bright Data SERP capability is also used to find companies or products that may already be solving the problem.

GapRadar creates competitor-oriented search queries and uses Bright Data SERP results to identify potential competitors.

### Competitor Flow

```text
User hypothesis
        ↓
GapRadar generates competitor searches
        ↓
Google Search
        ↓
Bright Data SERP API
        ↓
Structured organic results
        ↓
GapRadar identifies potential competitors
        ↓
Competitor Candidates
```

This helps answer:

> **“Is this an unsolved gap, or are companies already operating in this space?”**

---

# Complete Bright Data Flow Inside GapRadar

Bright Data powers both major product modes.

## Discovery Mode

```text
Razorpay Fix My Itch
        ↓
Bright Data Scraper Studio
        ↓
Market problems
        ↓
RecallGuard
        ↓
Opportunity Engine
        ↓
Ranked market opportunities
```

## Investigation Mode

A user enters a hypothesis.

### Research Evidence

```text
User hypothesis
        ↓
Research queries
        ↓
arXiv
        ↓
Bright Data arXiv collector
        ↓
Academic research evidence
```

### Demand + Competition Evidence

```text
User hypothesis
        ↓
Web queries
        ↓
Google Search
        ↓
Bright Data SERP API
        ↓
Demand evidence + competitor candidates
```

GapRadar then combines these evidence streams into one investigation that the user can inspect.

---

# Why Bright Data Matters to GapRadar

Without Bright Data, GapRadar would be limited to static, cached, or manually supplied information.

Bright Data connects GapRadar to the **live public web**.

It gives GapRadar three important types of market intelligence:

| Bright Data Usage | Source | What GapRadar Gets |
|---|---|---|
| **Scraper Studio** | Razorpay Fix My Itch | Real-world market problems |
| **arXiv Collector** | arXiv | Academic research evidence |
| **SERP API** | Google Search / public web | Demand evidence and competitor candidates |

GapRadar then adds the layers above that raw web data:

- reliability checking,
- evidence validation,
- evidence classification,
- opportunity analysis,
- hypothesis investigation,
- and delivery through the Web UI, MCP, and CLI.

> **Bright Data gives GapRadar its eyes on the live web. GapRadar turns that web data into trustworthy opportunity intelligence.**
