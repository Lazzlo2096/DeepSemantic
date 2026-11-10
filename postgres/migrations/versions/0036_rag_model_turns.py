"""Persist assistant messages, tool requests and provider-visible reasoning."""

from alembic import op
import sqlalchemy as sa


revision = "0036_rag_model_turns"
down_revision = "0035_rag_run_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_model_turns",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("rag_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("requested_tools", sa.JSON(), nullable=False),
        sa.Column("reasoning", sa.JSON(), nullable=False),
        sa.Column("reasoning_available", sa.Boolean(), nullable=False),
        sa.CheckConstraint("position > 0 AND phase IN ('research', 'answer')"),
    )


def downgrade() -> None:
    op.drop_table("rag_model_turns")
