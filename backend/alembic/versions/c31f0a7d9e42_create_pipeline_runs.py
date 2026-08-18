"""create pipeline_runs

Revision ID: c31f0a7d9e42
Revises: 9b1547994c7d
Create Date: 2026-08-18 21:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c31f0a7d9e42"
down_revision: str | Sequence[str] | None = "9b1547994c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The statuses that mean "this execution has not reached a verdict yet",
# spelled out rather than imported from app.domain.enums: a migration has
# to keep describing the schema it created even after the application's
# enum moves on. The application-side copy lives in
# app.db.models.pipeline_run.ACTIVE_PIPELINE_RUN_STATUSES.
#
# UPPERCASE because SQLAlchemy's Enum persists the member NAME, not its
# value: the column holds 'WAITING_PROVIDER' while the API serializes
# 'waiting_provider'. Lowercase here would match no row, leaving a valid
# index that enforces nothing.
ACTIVE_STATUS_PREDICATE = (
    "status IN ("
    "'QUEUED', 'COLLECTING', 'WAITING_PROVIDER', "
    "'VALIDATING', 'INGESTING', 'VERIFYING'"
    ")"
)


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
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
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
        sa.Column("collector_run_id", sa.Uuid(), nullable=True),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("trusted", sa.Boolean(), nullable=True),
        sa.Column("reliability_state", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["collector_id"],
            ["collectors.id"],
            name=op.f("fk_pipeline_runs_collector_id_collectors"),
        ),
        sa.ForeignKeyConstraint(
            ["collector_run_id"],
            ["collector_runs.id"],
            name=op.f("fk_pipeline_runs_collector_run_id_collector_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["reliability_incidents.id"],
            name=op.f("fk_pipeline_runs_incident_id_reliability_incidents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_runs")),
    )
    op.create_index("ix_pipeline_runs_collector_id", "pipeline_runs", ["collector_id"])
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    # Unique where present. NULLs are distinct, so the manual endpoint --
    # which sends no key -- is unconstrained, while a scheduler that does
    # send one gets a database guarantee rather than a check that races.
    op.create_index(
        "uq_pipeline_runs_idempotency_key",
        "pipeline_runs",
        ["idempotency_key"],
        unique=True,
    )
    # At most one ACTIVE execution per collector, enforced by PostgreSQL.
    # The service cannot provide this on its own: two concurrent claims
    # can both observe "no active run" before either inserts, and both
    # would then start a Bright Data collection over the same source.
    # Partial, so terminal rows are excluded and run history stays
    # unbounded. Kept in step with
    # app.db.models.pipeline_run.ACTIVE_PIPELINE_RUN_STATUSES.
    op.create_index(
        "uq_pipeline_runs_active_collector",
        "pipeline_runs",
        ["collector_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_STATUS_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_pipeline_runs_active_collector", table_name="pipeline_runs")
    op.drop_index("uq_pipeline_runs_idempotency_key", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_status", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_collector_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
