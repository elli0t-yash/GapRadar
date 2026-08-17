# GapRadar

> Discover gaps. Prove the data behind them.

GapRadar discovers product and startup opportunities from public-web pain
signals.

Bright Data Scraper Studio collects the source data. RecallGuard protects
the intelligence pipeline from silent extraction degradation by detecting
failures, validating scraper repairs, and requiring fresh verification
before data is trusted again.

## Architecture

Public Web
→ Bright Data Scraper Studio
→ GapRadar Ingestion
→ RecallGuard
→ Trusted Signals
→ Opportunity Engine
→ PostgreSQL
→ FastAPI
→ React Dashboard

Harness AI Agent acts as an advisory investigator during reliability
incidents.

## Runtime Stack

- Python
- FastAPI
- PostgreSQL
- Bright Data Scraper Studio
- Harness AI Agent
- React
- TypeScript

## Repository

- `backend/` — deployed GapRadar backend
- `frontend/` — deployed React frontend
- `external/` — Bright Data and Harness platform artifacts/configuration
- `demo/` — hackathon scenarios and Healing Proof examples
- `docs/` — architecture and product documentation
- `scripts/` — development/demo utilities

## Reliability Principle

AI proposes. RecallGuard proves.

APPROVED != RECOVERED

A scraper repair is considered recovered only after a fresh independent
execution passes all mandatory RecallGuard validation gates.
