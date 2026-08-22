# GapRadar Product Overview

GapRadar is a trust-aware opportunity intelligence platform.

It answers two questions:

1. What market gaps are emerging from live public-web signals?
2. Can we trust the underlying data enough to act on it?

## Product Modes

### 1) Discovery Mode

Discovery mode ingests structured market-problem records and ranks opportunities.

High-level flow:

```text
Public source page
  -> Bright Data Scraper Studio
  -> GapRadar ingestion + validation
  -> RecallGuard trust checks
  -> Opportunity Engine ranking
  -> Dashboard opportunities
```

The current primary discovery source is Razorpay Fix My Itch.

### 2) Investigation Mode

Investigation mode lets users submit a hypothesis and ask GapRadar to gather evidence around it.

High-level flow:

```text
User hypothesis
  -> Investigation run
  -> Research evidence (arXiv)
  -> Demand evidence (SERP)
  -> Competitor candidates (SERP)
  -> Unified investigation view
```

This mode separates three evidence streams:

- academic/technical feasibility
- real-world demand signals
- existing competitive landscape

## Trust and Reliability

GapRadar does not treat successful scraping as trustworthy data by default.

RecallGuard sits between acquisition and decision-making:

- detects extraction drift
- tracks reliability incidents
- blocks untrusted signals from the opportunity surface
- requires fresh verification before considering a repair recovered

Core principle:

```text
APPROVED != RECOVERED
```

## Interfaces

GapRadar is accessible through three interfaces backed by the same core services:

- Web UI
- MCP server (tooling for agents)
- CLI (MCP-backed)
