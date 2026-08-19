"""add per-query progress and partial-run warning to research enrichments

Adds the two columns that make an enrichment observable while it runs:

- `query_states`: a JSONB snapshot of each research query's status,
  provider job id, record count and timings, rewritten on every
  transition. It is what the frontend polls to say "2 of 3 searches
  complete" without inventing progress on a timer.
- `warning`: set alongside SUCCEEDED when some searches returned and
  others did not, so a partial run is still shown as useful research
  while staying honest about the gap.

Both are additive and nullable-or-defaulted, so existing rows -- runs
that completed before per-query tracking existed -- remain readable and
simply report no per-query detail.

Revision ID: d92a5c14e7b3
Revises: b4c81e37f9a2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d92a5c14e7b3"
down_revision: str | Sequence[str] | None = "b4c81e37f9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_enrichment_runs",
        sa.Column("warning", sa.Text(), nullable=True),
    )
    op.add_column(
        "research_enrichment_runs",
        sa.Column(
            "query_states",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("research_enrichment_runs", "query_states")
    op.drop_column("research_enrichment_runs", "warning")
