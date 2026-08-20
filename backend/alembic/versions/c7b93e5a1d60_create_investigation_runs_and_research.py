"""create investigation runs and investigation research matches

Revision ID: c7b93e5a1d60
Revises: a1f7c2d8b904
Create Date: 2026-08-20 12:20:00.000000

The execution and persistence backbone for independent investigations.

Three additive changes:

1. `investigation_runs` -- one execution attempt against one
   Investigation, with the same partial unique index over active statuses
   that stops research enrichments from starting twice.

2. `investigation_research_matches` -- relevance verdicts for a
   user-supplied hypothesis. A SEPARATE table from
   `opportunity_research_matches` so both sides keep real foreign keys,
   so each uniqueness rule is actually enforced, and above all so a
   verdict about a user hypothesis and a verdict about validated market
   evidence can never overwrite one another. `research_papers` is
   untouched and stays globally unique on arxiv_id: a paper is stored
   once no matter which kind of subject found it, and only the JUDGEMENT
   is per-subject.

3. `research_search_runs.investigation_id` -- so a search can be
   attributed to an investigation -- plus a CHECK asserting that EXACTLY
   ONE subject column is populated.

   THIS ONE IS NOT PURELY ADDITIVE, and the docstring says so rather than
   hiding it. The constraint rejects both-set (a row that names two
   subjects) AND both-null (a row that names none). Both-null was legal
   before this revision, so a deployment holding such rows cannot take
   this migration until they are attributed or removed.

   That case is detected and reported by the pre-flight guard in
   `upgrade()` instead of surfacing as a raw constraint violation, and
   NOTHING IS DELETED OR REWRITTEN automatically: which of those rows is
   an operator probe worth keeping and which is junk is a judgement this
   migration has no basis to make.

The other two changes are additive. Nothing existing is dropped.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7b93e5a1d60"
down_revision: str | Sequence[str] | None = "a1f7c2d8b904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The statuses that mean "this run has not reached a verdict yet",
# spelled out rather than imported from app.domain.enums: a migration has
# to keep describing the schema it created even after the application's
# enum moves on. The application-side copy lives in
# app.db.models.investigation_run.ACTIVE_INVESTIGATION_RUN_STATUSES.
#
# UPPERCASE because SQLAlchemy's Enum persists the member NAME: the
# column holds 'RUNNING' while the API serialises 'running'. Lowercase
# here would match no row, leaving a valid index that enforces nothing --
# the worst outcome for a constraint whose whole job is to stop a second
# billable provider run.
ACTIVE_RUN_PREDICATE = "status IN ('QUEUED', 'RUNNING')"

# A search belongs to EXACTLY one subject. XOR over the two IS NOT NULL
# tests: both-set and both-null are equally rejected. The application-side
# copy lives on ResearchSearchRun.__table_args__.
SINGLE_SUBJECT_PREDICATE = "(signal_id IS NOT NULL) <> (investigation_id IS NOT NULL)"

# Rows that the constraint above would reject. Only both-null is possible
# on a pre-revision database, because investigation_id does not exist yet.
UNATTRIBUTED_ROWS_QUERY = (
    "SELECT count(*) FROM research_search_runs WHERE signal_id IS NULL"
)


def upgrade() -> None:
    op.create_table(
        "investigation_runs",
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
        # VARCHAR(32) holding the enum member NAME, the convention every
        # other status column in this schema uses.
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("warning", sa.Text(), nullable=True),
        sa.Column(
            "query_states",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("outcome_reason", sa.String(length=48), nullable=True),
        sa.Column(
            "counters",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_runs_investigation_id_investigations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_runs")),
    )
    op.create_index(
        "ix_investigation_runs_investigation_id",
        "investigation_runs",
        ["investigation_id"],
    )
    op.create_index("ix_investigation_runs_status", "investigation_runs", ["status"])
    # AT MOST ONE ACTIVE RUN PER INVESTIGATION, enforced by PostgreSQL.
    # The service cannot provide this alone: two clicks or two tabs can
    # both observe "nothing running" before either inserts, and both
    # would then start their own Bright Data searches and their own LLM
    # calls over the same investigation. Partial, so terminal rows are
    # excluded and history stays unbounded.
    op.create_index(
        "uq_investigation_runs_active_investigation",
        "investigation_runs",
        ["investigation_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_RUN_PREDICATE),
    )

    op.create_table(
        "investigation_research_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("research_paper_id", sa.Uuid(), nullable=False),
        # NOT NULL: a match with no relevance score is an assertion with
        # no evidence, and this project does not store those.
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("technical_readiness_score", sa.Float(), nullable=True),
        sa.Column(
            "matched_concepts",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f(
                "fk_investigation_research_matches_investigation_id_investigations"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["research_paper_id"],
            ["research_papers.id"],
            name=op.f(
                "fk_investigation_research_matches_research_paper_id_research_papers"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_research_matches")),
        # One verdict per (investigation, paper). Re-running updates the
        # existing row instead of stacking near-duplicate claims.
        sa.UniqueConstraint(
            "investigation_id",
            "research_paper_id",
            name="uq_investigation_research_matches_investigation_paper",
        ),
    )
    op.create_index(
        "ix_investigation_research_matches_investigation_id",
        "investigation_research_matches",
        ["investigation_id"],
    )
    op.create_index(
        "ix_investigation_research_matches_paper_id",
        "investigation_research_matches",
        ["research_paper_id"],
    )
    op.create_index(
        "ix_investigation_research_matches_relevance",
        "investigation_research_matches",
        ["investigation_id", "relevance_score"],
    )

    # Search provenance for the second kind of subject. Nullable, so
    # every existing row stays valid and nothing is backfilled.
    op.add_column(
        "research_search_runs",
        sa.Column("investigation_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_research_search_runs_investigation_id_investigations"),
        "research_search_runs",
        "investigations",
        ["investigation_id"],
        ["id"],
    )
    op.create_index(
        "ix_research_search_runs_investigation_id",
        "research_search_runs",
        ["investigation_id"],
    )
    _refuse_unattributed_rows()
    op.create_check_constraint(
        "ck_research_search_runs_single_subject",
        "research_search_runs",
        sa.text(SINGLE_SUBJECT_PREDICATE),
    )


def _refuse_unattributed_rows() -> None:
    """Stop with an actionable message rather than a constraint violation.

    Checked BEFORE the column is added, so the only way to fail here is
    pre-existing rows with no signal_id -- searches that were legal when
    they were written and are not legal now.

    Deliberately raises instead of deleting or backfilling. This
    migration cannot tell an operator probe worth attributing from a row
    worth dropping, and guessing would destroy provenance to make a
    deploy quieter.
    """
    orphans = op.get_bind().execute(sa.text(UNATTRIBUTED_ROWS_QUERY)).scalar_one()
    if orphans:
        raise RuntimeError(
            f"{orphans} research_search_runs row(s) have no signal_id. This "
            "revision requires every search to name exactly one subject. "
            "Inspect them with:\n"
            "    SELECT id, query, searched_at FROM research_search_runs "
            "WHERE signal_id IS NULL;\n"
            "then either attribute each row to its signal, or delete the "
            "ones that are not worth keeping, and re-run the migration. "
            "Nothing has been changed."
        )


def downgrade() -> None:
    op.drop_constraint(
        "ck_research_search_runs_single_subject",
        "research_search_runs",
        type_="check",
    )
    op.drop_index(
        "ix_research_search_runs_investigation_id", table_name="research_search_runs"
    )
    op.drop_constraint(
        op.f("fk_research_search_runs_investigation_id_investigations"),
        "research_search_runs",
        type_="foreignkey",
    )
    op.drop_column("research_search_runs", "investigation_id")

    op.drop_index(
        "ix_investigation_research_matches_relevance",
        table_name="investigation_research_matches",
    )
    op.drop_index(
        "ix_investigation_research_matches_paper_id",
        table_name="investigation_research_matches",
    )
    op.drop_index(
        "ix_investigation_research_matches_investigation_id",
        table_name="investigation_research_matches",
    )
    op.drop_table("investigation_research_matches")

    op.drop_index(
        "uq_investigation_runs_active_investigation",
        table_name="investigation_runs",
    )
    op.drop_index("ix_investigation_runs_status", table_name="investigation_runs")
    op.drop_index(
        "ix_investigation_runs_investigation_id", table_name="investigation_runs"
    )
    op.drop_table("investigation_runs")
