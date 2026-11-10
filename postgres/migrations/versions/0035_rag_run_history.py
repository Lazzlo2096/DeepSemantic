"""Persist RAG runs and their tool calls."""

from alembic import op
import sqlalchemy as sa


revision = "0035_rag_run_history"
down_revision = "0034_index_chunk_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("answer", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('running', 'answered', 'insufficient_evidence', 'failed')"),
    )
    op.create_table(
        "rag_tool_calls",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("rag_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("position > 0 AND duration_ms >= 0"),
    )


def downgrade() -> None:
    op.drop_table("rag_tool_calls")
    op.drop_table("rag_runs")
