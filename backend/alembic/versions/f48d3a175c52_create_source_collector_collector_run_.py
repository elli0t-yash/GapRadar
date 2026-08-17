"""create source collector collector_run signal tables

Revision ID: f48d3a175c52
Revises:
Create Date: 2026-08-17 10:20:27.481463

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f48d3a175c52"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
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
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
    )
    op.create_index("ix_sources_name", "sources", ["name"])

    op.create_table(
        "collectors",
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
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("external_collector_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_collectors_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collectors")),
        sa.UniqueConstraint(
            "provider",
            "external_collector_id",
            name="uq_collectors_provider_external_collector_id",
        ),
    )
    op.create_index("ix_collectors_source_id", "collectors", ["source_id"])

    op.create_table(
        "collector_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("external_run_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column(
            "raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["collector_id"],
            ["collectors.id"],
            name=op.f("fk_collector_runs_collector_id_collectors"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_runs")),
        sa.UniqueConstraint(
            "collector_id",
            "external_run_id",
            name="uq_collector_runs_collector_external_run_id",
        ),
    )
    op.create_index(
        "ix_collector_runs_collector_id", "collector_runs", ["collector_id"]
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("collector_run_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_signals_source_id_sources")
        ),
        sa.ForeignKeyConstraint(
            ["collector_run_id"],
            ["collector_runs.id"],
            name=op.f("fk_signals_collector_run_id_collector_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signals")),
        sa.UniqueConstraint(
            "source_id", "external_id", name="uq_signals_source_external_id"
        ),
    )
    op.create_index("ix_signals_source_id", "signals", ["source_id"])
    op.create_index("ix_signals_collector_run_id", "signals", ["collector_run_id"])
    op.create_index("ix_signals_canonical_url", "signals", ["canonical_url"])
    op.create_index("ix_signals_observed_at", "signals", ["observed_at"])
    op.create_index("ix_signals_signal_type", "signals", ["signal_type"])


def downgrade() -> None:
    op.drop_table("signals")
    op.drop_table("collector_runs")
    op.drop_table("collectors")
    op.drop_table("sources")
