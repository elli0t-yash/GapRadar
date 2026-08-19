"""add typed outcome reason and funnel counters to research enrichments

Two additions, both driven by the same defect: the UI could not tell
DISCOVERED papers from JUDGED papers, because only the discovered count
and the accepted matches were ever persisted.

- `counters`: {discovered, selected, judged, matched} for one run. Three
  of those four are unrecoverable after the fact -- rejected verdicts are
  not stored -- so they are written when the run ends.
- `outcome_reason`: why the run ended, as a value the frontend can branch
  on instead of parsing an English error string. Notably it carries
  whether a retry could plausibly change anything.

Both are additive with defaults, so rows written before this migration
stay readable: they report no reason and an empty counter map, and the
read layer treats that as "not recorded" rather than as zero.

Revision ID: e5c3b81a4f26
Revises: d92a5c14e7b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c3b81a4f26"
down_revision: str | Sequence[str] | None = "d92a5c14e7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_enrichment_runs",
        sa.Column(
            "outcome_reason",
            sa.Enum(
                "NO_RELEVANT_RESEARCH",
                "ACQUISITION_PARTIAL",
                "QUERY_PLAN_UNAVAILABLE",
                "OPPORTUNITY_MISSING",
                "QUERY_GENERATION_PROVIDER_ERROR",
                "ACQUISITION_FAILED",
                "SEMANTIC_MATCHING_FAILED",
                "TIMEOUT",
                "INTERRUPTED",
                "UNEXPECTED_ERROR",
                name="research_outcome_reason",
                native_enum=False,
                length=48,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "research_enrichment_runs",
        sa.Column(
            "counters",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("research_enrichment_runs", "counters")
    op.drop_column("research_enrichment_runs", "outcome_reason")
