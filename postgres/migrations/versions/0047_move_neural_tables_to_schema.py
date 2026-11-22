"""Move neural-processing tables into their own schema."""

from alembic import op


revision = "0047_move_neural_schema"
down_revision = "0046_move_arxiv_schema"
branch_labels = None
depends_on = None


SCHEMA = "neural_things"
TABLES = (
    "chunks",
    "paper_chunks",
    "paper_summaries",
    "pipeline_settings",
    "rag_model_turns",
    "rag_runs",
    "rag_tool_calls",
)


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    for table in TABLES:
        op.execute(f'ALTER TABLE public."{table}" SET SCHEMA "{SCHEMA}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f'ALTER TABLE "{SCHEMA}"."{table}" SET SCHEMA public')
    op.execute(f'DROP SCHEMA "{SCHEMA}"')
