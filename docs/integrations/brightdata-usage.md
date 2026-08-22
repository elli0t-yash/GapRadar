# Bright Data Usage in GapRadar

GapRadar uses Bright Data in three distinct integration paths.

## 1) Discovery source collection (Scraper Studio)

Collector: `gapradar-fix-my-itch`

Purpose:

- Collect structured market-problem records from Razorpay Fix My Itch.

Typical fields used downstream:

- problem
- description
- industry
- itch/severity/tam/whitespace/frequency scores
- source metadata

## 2) Investigation research collection (Scraper Studio)

Collector: `gapradar-arxiv-research-v1`

Purpose:

- Collect structured paper metadata from arXiv result pages using runtime-generated search URLs.

Typical fields used downstream:

- arxiv_id
- title
- abstract
- authors
- categories
- published_at
- paper_url/pdf_url

## 3) Investigation web evidence (SERP API)

Product: Bright Data SERP API

Purpose:

- Collect structured organic result metadata for demand evidence and competitor discovery.

Typical normalized fields used downstream:

- query
- title
- url (normalized)
- domain
- snippet
- position

## Why this split matters

- Scraper Studio collectors are custom extraction logic for specific surfaces.
- SERP API is a product API integration, not a Scraper Studio collector.
- GapRadar documents these separately to keep implementation boundaries clear.
