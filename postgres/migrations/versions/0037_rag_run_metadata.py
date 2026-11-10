"""Persist model and prompt metadata for RAG runs."""

from alembic import op
import sqlalchemy as sa


revision = "0037_rag_run_metadata"
down_revision = "0036_rag_model_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rag_runs", sa.Column("model", sa.Text()))
    op.add_column("rag_runs", sa.Column("reasoning_effort", sa.Text()))
    op.add_column("rag_runs", sa.Column("prompt_version", sa.Text()))


def downgrade() -> None:
    op.drop_column("rag_runs", "prompt_version")
    op.drop_column("rag_runs", "reasoning_effort")
    op.drop_column("rag_runs", "model")
