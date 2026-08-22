# Public Data and Access Compliance

GapRadar is designed for public-web intelligence.

## Data Access Policy

GapRadar does not intentionally scrape:

- private account areas
- login-gated content
- paywalled content bodies
- credential-protected systems
- content obtained by bypassing access controls

## Current public sources

- Razorpay Fix My Itch public page
- arXiv public search/result pages
- SERP API organic search-result metadata

## Operational safeguards

- Discovery and investigation flows persist provenance metadata.
- Read endpoints are separated from provider-spending actions.
- Trust and reliability gates prevent low-confidence extraction from silently entering decision surfaces.

## Contributor note

When adding new sources, keep this checklist:

1. Verify the source is publicly accessible without credentials.
2. Confirm Terms of Use and legal constraints for your context.
3. Preserve provenance fields so evidence remains traceable.
4. Add source-specific validation before promoting records to trusted surfaces.
