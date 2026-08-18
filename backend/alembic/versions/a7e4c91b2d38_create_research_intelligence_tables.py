"""create research intelligence tables

Revision ID: a7e4c91b2d38
Revises: c31f0a7d9e42
Create Date: 2026-08-19 01:05:00.000000

Adds the research side's storage and provenance foundation:

- research_papers          one row per paper, keyed by arxiv_id
- research_search_runs     who asked, what was asked, when
- research_search_results  which papers one search returned, in order
- opportunity_research_matches  many-to-many between a Signal and a paper

Additive only. Nothing existing is altered, so the market pipeline
(sources -> collectors -> collector_runs -> signals -> opportunities) is
untouched by this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e4c91b2d38"
down_revision: str | Sequence[str] | None = "c31f0a7d9e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_papers",
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
        sa.Column("arxiv_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("authors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("primary_category_code", sa.String(length=64), nullable=True),
        # A calendar DATE, not a timestamp: arXiv publishes "2026-08-13"
        # with no time and no timezone, and neither is invented here.
        sa.Column("published_at", sa.Date(), nullable=False),
        sa.Column("paper_url", sa.String(length=2048), nullable=False),
        sa.Column("pdf_url", sa.String(length=2048), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_papers")),
        # Identity. One row per paper no matter how many searches find it,
        # and the constraint the ingestion upsert relies on to resolve a
        # concurrent double-insert into an update.
        sa.UniqueConstraint("arxiv_id", name="uq_research_papers_arxiv_id"),
    )
    op.create_index(
        "ix_research_papers_published_at", "research_papers", ["published_at"]
    )
    op.create_index(
        "ix_research_papers_primary_category_code",
        "research_papers",
        ["primary_category_code"],
    )
    op.create_index("ix_research_papers_source", "research_papers", ["source"])

    op.create_table(
        "research_search_runs",
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
        # Nullable: a search may be run without an originating opportunity
        # (an operator probe or a backfill), and recording those as
        # orphaned beats refusing to record them at all.
        sa.Column("signal_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("query", sa.String(length=512), nullable=False),
        sa.Column("searched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signals.id"],
            name=op.f("fk_research_search_runs_signal_id_signals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_search_runs")),
    )
    op.create_index(
        "ix_research_search_runs_signal_id", "research_search_runs", ["signal_id"]
    )
    op.create_index(
        "ix_research_search_runs_searched_at", "research_search_runs", ["searched_at"]
    )
    op.create_index(
        "ix_research_search_runs_source_query",
        "research_search_runs",
        ["source", "query"],
    )

    op.create_table(
        "research_search_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("research_search_run_id", sa.Uuid(), nullable=False),
        sa.Column("research_paper_id", sa.Uuid(), nullable=False),
        # 0-based index within the batch the provider returned. Search
        # order is a real relevance signal and is lost once unpacked.
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["research_search_run_id"],
            ["research_search_runs.id"],
            name=op.f(
                "fk_research_search_results_research_search_run_id_research_search_runs"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["research_paper_id"],
            ["research_papers.id"],
            name=op.f("fk_research_search_results_research_paper_id_research_papers"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_search_results")),
        # A paper appears at most once per search.
        sa.UniqueConstraint(
            "research_search_run_id",
            "research_paper_id",
            name="uq_research_search_results_run_paper",
        ),
    )
    op.create_index(
        "ix_research_search_results_run_id",
        "research_search_results",
        ["research_search_run_id"],
    )
    op.create_index(
        "ix_research_search_results_paper_id",
        "research_search_results",
        ["research_paper_id"],
    )

    op.create_table(
        "opportunity_research_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Points at signals, not at an "opportunities" table -- there is
        # none. An Opportunity is a read model computed over a trusted
        # Signal, so the Signal is the only durable identity to anchor to.
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("research_paper_id", sa.Uuid(), nullable=False),
        # NOT NULL: a match with no relevance score is an assertion with
        # no evidence.
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("technical_readiness_score", sa.Float(), nullable=True),
        sa.Column(
            "matched_concepts", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signals.id"],
            name=op.f("fk_opportunity_research_matches_signal_id_signals"),
        ),
        sa.ForeignKeyConstraint(
            ["research_paper_id"],
            ["research_papers.id"],
            name=op.f(
                "fk_opportunity_research_matches_research_paper_id_research_papers"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opportunity_research_matches")),
        # One verdict per (opportunity, paper). Re-running the matcher
        # updates the existing row rather than stacking near-duplicates.
        sa.UniqueConstraint(
            "signal_id",
            "research_paper_id",
            name="uq_opportunity_research_matches_signal_paper",
        ),
    )
    op.create_index(
        "ix_opportunity_research_matches_signal_id",
        "opportunity_research_matches",
        ["signal_id"],
    )
    op.create_index(
        "ix_opportunity_research_matches_paper_id",
        "opportunity_research_matches",
        ["research_paper_id"],
    )
    op.create_index(
        "ix_opportunity_research_matches_signal_relevance",
        "opportunity_research_matches",
        ["signal_id", "relevance_score"],
    )


def downgrade() -> None:
    # Reverse creation order: the match and result tables reference
    # research_papers, so they go first.
    op.drop_index(
        "ix_opportunity_research_matches_signal_relevance",
        table_name="opportunity_research_matches",
    )
    op.drop_index(
        "ix_opportunity_research_matches_paper_id",
        table_name="opportunity_research_matches",
    )
    op.drop_index(
        "ix_opportunity_research_matches_signal_id",
        table_name="opportunity_research_matches",
    )
    op.drop_table("opportunity_research_matches")

    op.drop_index(
        "ix_research_search_results_paper_id", table_name="research_search_results"
    )
    op.drop_index(
        "ix_research_search_results_run_id", table_name="research_search_results"
    )
    op.drop_table("research_search_results")

    op.drop_index(
        "ix_research_search_runs_source_query", table_name="research_search_runs"
    )
    op.drop_index(
        "ix_research_search_runs_searched_at", table_name="research_search_runs"
    )
    op.drop_index(
        "ix_research_search_runs_signal_id", table_name="research_search_runs"
    )
    op.drop_table("research_search_runs")

    op.drop_index("ix_research_papers_source", table_name="research_papers")
    op.drop_index(
        "ix_research_papers_primary_category_code", table_name="research_papers"
    )
    op.drop_index("ix_research_papers_published_at", table_name="research_papers")
    op.drop_table("research_papers")
