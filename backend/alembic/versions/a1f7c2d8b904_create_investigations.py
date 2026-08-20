"""create investigations

Revision ID: a1f7c2d8b904
Revises: e5c3b81a4f26
Create Date: 2026-08-20 10:40:00.000000

The foundation for independent investigations: user-supplied ideas,
problems and hypotheses that GapRadar can be asked to look into, held
apart from `signals`.

The separation is the design. `signals` holds externally discovered
evidence that survived collection and source-contract validation;
`investigations` holds text a user typed. Reusing the signals table so
that research code could be pointed at an investigation would have
permanently erased that difference for every consumer that reads
`signals` expecting collected, validated data.

Deliberately absent: any score, verdict or research result column.
Nothing computes those yet, and a column of NULLs would be
indistinguishable from a real result that happened to be missing.

Additive only. Nothing existing is altered.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f7c2d8b904"
down_revision: str | Sequence[str] | None = "e5c3b81a4f26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigations",
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
        # The user's wording, preserved verbatim apart from trimmed outer
        # whitespace. Text rather than a VARCHAR: the length ceiling is an
        # API decision (app.investigations.schemas.MAX_QUERY_CHARS) that
        # will be tuned, and a column width is the wrong place to freeze
        # it -- lowering one later is a destructive migration.
        sa.Column("query", sa.Text(), nullable=False),
        # Nullable because nothing produces them yet. The create API does
        # not accept them and GapRadar does not derive them in this phase.
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        # VARCHAR(32) holding the InvestigationStatus member NAME, the
        # same convention the other status columns in this schema use:
        # these are non-native SQLAlchemy Enums, so the database stores
        # 'DRAFT' while the API serialises 'draft'.
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
    )
    # Recent-first listing is this table's only read path today.
    op.create_index("ix_investigations_created_at", "investigations", ["created_at"])
    op.create_index("ix_investigations_status", "investigations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_investigations_status", table_name="investigations")
    op.drop_index("ix_investigations_created_at", table_name="investigations")
    op.drop_table("investigations")
