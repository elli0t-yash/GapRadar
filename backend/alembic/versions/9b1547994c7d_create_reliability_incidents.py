"""create reliability_incidents

Revision ID: 9b1547994c7d
Revises: f48d3a175c52
Create Date: 2026-08-18 11:13:25.491540

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b1547994c7d"
down_revision: str | Sequence[str] | None = "f48d3a175c52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reliability_incidents",
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
        sa.Column("detection_run_id", sa.Uuid(), nullable=True),
        sa.Column("verification_run_id", sa.Uuid(), nullable=True),
        # The enum columns mirror the existing tables' representation:
        # non-native SQLAlchemy Enums, persisted as VARCHAR(32) holding
        # the member name, with no CHECK constraint.
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("recommended_action", sa.String(length=32), nullable=False),
        sa.Column("repair_attempts", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "recovery_proof", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["collector_id"],
            ["collectors.id"],
            name=op.f("fk_reliability_incidents_collector_id_collectors"),
        ),
        sa.ForeignKeyConstraint(
            ["detection_run_id"],
            ["collector_runs.id"],
            name=op.f("fk_reliability_incidents_detection_run_id_collector_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["verification_run_id"],
            ["collector_runs.id"],
            name=op.f("fk_reliability_incidents_verification_run_id_collector_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reliability_incidents")),
    )
    op.create_index(
        "ix_reliability_incidents_collector_id",
        "reliability_incidents",
        ["collector_id"],
    )
    op.create_index(
        "ix_reliability_incidents_status", "reliability_incidents", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_reliability_incidents_status", table_name="reliability_incidents")
    op.drop_index(
        "ix_reliability_incidents_collector_id", table_name="reliability_incidents"
    )
    op.drop_table("reliability_incidents")
