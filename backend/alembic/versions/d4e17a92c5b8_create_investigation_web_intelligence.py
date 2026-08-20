"""create investigation web intelligence

Revision ID: d4e17a92c5b8
Revises: c7b93e5a1d60
Create Date: 2026-08-20 15:10:00.000000

Web discovery for investigations: what was searched, what came back, and
what it means. Four new tables plus one new column, all additive.

    investigation_web_search_runs   one provider execution. Observability.
                                    CHECK: family is one the engine runs.
    investigation_web_search_hits   one URL per search, at one rank.
                                    Provenance.
    investigation_demand_evidence   one judgement per (investigation, url)
    investigation_competitors       one judgement per (investigation, url)

    investigation_runs.phases       typed phase-by-phase progress

The split between hits and evidence is the design. Hits accumulate and
are never updated -- "this search returned this page at rank 3" is a fact
about a moment. Evidence is upserted per (investigation, url), so
re-running revises the verdict without duplicating the finding and
without losing which searches converged on it.

Hits reference a URL rather than a foreign key to an evidence row: a hit
may belong to a demand search, a competitor search, or both, and a
polymorphic reference would have no foreign key at all.

Additive only. Nothing existing is altered or dropped. The new `phases`
column is NOT NULL with a server default, so every existing run row stays
valid without a backfill.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e17a92c5b8"
down_revision: str | Sequence[str] | None = "c7b93e5a1d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

# The families a persisted search run may name, spelled out rather than
# imported from app.web_intelligence.schemas: a migration has to keep
# describing the schema it created even after the application's enum
# moves on. The application-side copy is
# app.db.models.investigation_web.WEB_SEARCH_FAMILY_PREDICATE, and a test
# pins the two together.
#
# LOWERCASE, deliberately. `family` is a bare VARCHAR written from
# WebSearchFamily.value -- unlike the status columns in this same
# migration, which are non-native SQLAlchemy Enums and persist the member
# NAME in uppercase. Uppercase here would match no row and leave a
# constraint that rejects every insert.
WEB_SEARCH_FAMILY_PREDICATE = "family IN ('competitor', 'demand')"


def upgrade() -> None:
    op.create_table(
        "investigation_web_search_runs",
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
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        # Nullable: a search may be replayed or backfilled outside a run,
        # and refusing to record that would push it somewhere with no
        # provenance at all.
        sa.Column("investigation_run_id", sa.Uuid(), nullable=True),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("query", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("product", sa.String(length=64), nullable=False),
        sa.Column("locale_country", sa.String(length=2), nullable=False),
        sa.Column("locale_language", sa.String(length=2), nullable=False),
        # VARCHAR(32) holding the enum member NAME, the convention every
        # other status column in this schema uses.
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "records_returned", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Null unless the provider issues one. The synchronous SERP API
        # does not, and fabricating an id would make an untraceable
        # request look traceable.
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_web_search_runs_investigation_id"),
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            name=op.f("fk_investigation_web_search_runs_investigation_run_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_web_search_runs")),
        # A search run names one of the families the engine actually
        # runs. `family` is a bare string column, so without this a typo
        # or a future writer could persist a row that every family-scoped
        # read silently skips -- a paid provider request that no phase
        # counts and no evidence hangs off.
        sa.CheckConstraint(
            WEB_SEARCH_FAMILY_PREDICATE,
            name="ck_investigation_web_search_runs_family",
        ),
    )
    op.create_index(
        "ix_investigation_web_search_runs_investigation_id",
        "investigation_web_search_runs",
        ["investigation_id"],
    )
    op.create_index(
        "ix_investigation_web_search_runs_run_id",
        "investigation_web_search_runs",
        ["investigation_run_id"],
    )
    op.create_index(
        "ix_investigation_web_search_runs_family",
        "investigation_web_search_runs",
        ["family"],
    )

    op.create_table(
        "investigation_web_search_hits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("investigation_web_search_run_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        # A calendar DATE, and only when the provider stated a reliable
        # absolute one. Never inferred.
        sa.Column("published_at", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(
            ["investigation_web_search_run_id"],
            ["investigation_web_search_runs.id"],
            name=op.f("fk_investigation_web_search_hits_run_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_web_search_hits")),
        # Within-query dedupe is applied during normalization; this is the
        # database refusing to let a second writer skip it.
        sa.UniqueConstraint(
            "investigation_web_search_run_id",
            "url",
            name="uq_investigation_web_search_hits_run_url",
        ),
    )
    op.create_index(
        "ix_investigation_web_search_hits_run_id",
        "investigation_web_search_hits",
        ["investigation_web_search_run_id"],
    )
    # The lookup that answers "which searches found this page?"
    op.create_index(
        "ix_investigation_web_search_hits_url",
        "investigation_web_search_hits",
        ["url"],
    )

    op.create_table(
        "investigation_demand_evidence",
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
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Date(), nullable=True),
        sa.Column("classification", sa.String(length=32), nullable=False),
        # NOT NULL: a verdict with no strength is an assertion with no
        # evidence, and this project does not store those.
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_demand_evidence_investigation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_demand_evidence")),
        # ONE ROW PER (investigation, url), whichever queries found it.
        # Counting a well-indexed page once per query that returned it
        # would let one blog post look like a market.
        sa.UniqueConstraint(
            "investigation_id",
            "url",
            name="uq_investigation_demand_evidence_investigation_url",
        ),
    )
    op.create_index(
        "ix_investigation_demand_evidence_investigation_id",
        "investigation_demand_evidence",
        ["investigation_id"],
    )
    op.create_index(
        "ix_investigation_demand_evidence_classification",
        "investigation_demand_evidence",
        ["investigation_id", "classification"],
    )

    op.create_table(
        "investigation_competitors",
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
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        # A DISPLAY IDENTITY, not a verified company name: discovery does
        # not open the page. There is deliberately no pricing, feature or
        # funding column, because nothing here could fill one honestly.
        sa.Column("name", sa.String(length=1024), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_competitors_investigation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_competitors")),
        sa.UniqueConstraint(
            "investigation_id",
            "url",
            name="uq_investigation_competitors_investigation_url",
        ),
    )
    op.create_index(
        "ix_investigation_competitors_investigation_id",
        "investigation_competitors",
        ["investigation_id"],
    )
    op.create_index(
        "ix_investigation_competitors_classification",
        "investigation_competitors",
        ["investigation_id", "classification"],
    )

    # Typed phase-by-phase progress. NOT NULL with a server default, so
    # every existing run row stays valid with no backfill; an old row
    # reads "{}" which the read model renders as every phase PENDING --
    # honest for a run that predates phases.
    op.add_column(
        "investigation_runs",
        sa.Column(
            "phases", JSONB, server_default=sa.text("'{}'"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("investigation_runs", "phases")

    op.drop_index(
        "ix_investigation_competitors_classification",
        table_name="investigation_competitors",
    )
    op.drop_index(
        "ix_investigation_competitors_investigation_id",
        table_name="investigation_competitors",
    )
    op.drop_table("investigation_competitors")

    op.drop_index(
        "ix_investigation_demand_evidence_classification",
        table_name="investigation_demand_evidence",
    )
    op.drop_index(
        "ix_investigation_demand_evidence_investigation_id",
        table_name="investigation_demand_evidence",
    )
    op.drop_table("investigation_demand_evidence")

    op.drop_index(
        "ix_investigation_web_search_hits_url",
        table_name="investigation_web_search_hits",
    )
    op.drop_index(
        "ix_investigation_web_search_hits_run_id",
        table_name="investigation_web_search_hits",
    )
    op.drop_table("investigation_web_search_hits")

    op.drop_index(
        "ix_investigation_web_search_runs_family",
        table_name="investigation_web_search_runs",
    )
    op.drop_index(
        "ix_investigation_web_search_runs_run_id",
        table_name="investigation_web_search_runs",
    )
    op.drop_index(
        "ix_investigation_web_search_runs_investigation_id",
        table_name="investigation_web_search_runs",
    )
    op.drop_table("investigation_web_search_runs")
