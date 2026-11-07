"""Store one summarization job and its final result per arXiv version."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0013_create_paper_summaries"
down_revision = "0012_report_paper_status_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_summaries",
        sa.Column("arxiv_id", sa.Text(), sa.ForeignKey("arxiv_papers.arxiv_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.Uuid(), nullable=False, unique=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("parts_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parts_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text()),
        sa.Column("prompt_version", sa.Text()),
        sa.Column("summary", JSONB()),
        sa.Column("last_error", sa.Text()),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="paper_summary_status"),
        sa.CheckConstraint("(status = 'running') = (lease_until IS NOT NULL)", name="paper_summary_lease"),
        sa.CheckConstraint("(status IN ('completed', 'failed')) = (finished_at IS NOT NULL)", name="paper_summary_finish"),
        sa.CheckConstraint("(status = 'completed') = (summary IS NOT NULL)", name="paper_summary_result"),
        sa.CheckConstraint("parts_completed BETWEEN 0 AND parts_total", name="paper_summary_progress"),
    )
    op.create_index(
        "paper_summaries_queued_idx", "paper_summaries", ["available_at", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "paper_summaries_running_idx", "paper_summaries", ["lease_until"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_table("paper_summaries")
