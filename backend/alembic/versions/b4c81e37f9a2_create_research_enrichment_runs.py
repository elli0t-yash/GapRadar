"""create research enrichment runs

Revision ID: b4c81e37f9a2
Revises: a7e4c91b2d38
Create Date: 2026-08-19 12:10:00.000000

The job record behind on-demand research enrichment. `GET /research` stays
a pure read of persisted intelligence; this table is the separate,
explicitly-created record of an acquisition a user asked for, and is what
the browser polls.

Additive only. Nothing existing is altered.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c81e37f9a2"
down_revision: str | Sequence[str] | None = "a7e4c91b2d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The statuses that mean "this job has not reached a verdict yet", spelled
# out rather than imported from app.domain.enums: a migration has to keep
# describing the schema it created even after the application's enum moves
# on. The application-side copy lives in
# app.db.models.research_enrichment_run.ACTIVE_ENRICHMENT_STATUSES.
#
# UPPERCASE because SQLAlchemy's Enum persists the member NAME, not its
# value: the column holds 'RUNNING' while the API serialises 'running'.
# Lowercase here would match no row, leaving a valid index that enforces
# nothing -- the worst outcome for a constraint whose whole job is to stop
# a second billable provider run.
ACTIVE_STATUS_PREDICATE = "status IN ('QUEUED', 'RUNNING')"


def upgrade() -> None:
    op.create_table(
        "research_enrichment_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signals.id"],
            name=op.f("fk_research_enrichment_runs_signal_id_signals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_enrichment_runs")),
    )
    op.create_index(
        "ix_research_enrichment_runs_signal_id",
        "research_enrichment_runs",
        ["signal_id"],
    )
    op.create_index(
        "ix_research_enrichment_runs_status", "research_enrichment_runs", ["status"]
    )
    # AT MOST ONE ACTIVE JOB PER OPPORTUNITY, enforced by PostgreSQL. The
    # service cannot provide this alone: two clicks or two tabs can both
    # observe "nothing running" before either inserts, and both would then
    # start their own Bright Data searches and their own LLM calls over the
    # same signal. Partial, so terminal rows are excluded and history stays
    # unbounded.
    op.create_index(
        "uq_research_enrichment_runs_active_signal",
        "research_enrichment_runs",
        ["signal_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_STATUS_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_research_enrichment_runs_active_signal",
        table_name="research_enrichment_runs",
    )
    op.drop_index(
        "ix_research_enrichment_runs_status", table_name="research_enrichment_runs"
    )
    op.drop_index(
        "ix_research_enrichment_runs_signal_id", table_name="research_enrichment_runs"
    )
    op.drop_table("research_enrichment_runs")
