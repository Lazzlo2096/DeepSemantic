"""Store paper summary statuses as a PostgreSQL enum."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0029_enum_paper_summary_status"
down_revision = "0028_arxiv_paper_text_lease"
branch_labels = None
depends_on = None


summary_status = postgresql.ENUM("queued", "running", "completed", "failed", name="paper_summary_status")
STATUS_CHECKS = {
    "paper_summary_lease": "(status = 'running') = (lease_until IS NOT NULL)",
    "paper_summary_finish": "(status IN ('completed', 'failed')) = (finished_at IS NOT NULL)",
    "paper_summary_result": "(status = 'completed') = (summary IS NOT NULL)",
}


def change_status_type(column_type: sa.types.TypeEngine, cast: str) -> None:
    for name in STATUS_CHECKS:
        op.drop_constraint(name, "paper_summaries", type_="check")
    op.drop_index("paper_summaries_queued_idx", table_name="paper_summaries")
    op.drop_index("paper_summaries_running_idx", table_name="paper_summaries")
    op.alter_column("paper_summaries", "status", server_default=None)
    op.alter_column("paper_summaries", "status", type_=column_type, postgresql_using=cast)
    op.alter_column("paper_summaries", "status", server_default="queued")

    for name, check in STATUS_CHECKS.items():
        op.create_check_constraint(name, "paper_summaries", check)
    op.create_index(
        "paper_summaries_queued_idx", "paper_summaries", ["available_at", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "paper_summaries_running_idx", "paper_summaries", ["lease_until"],
        postgresql_where=sa.text("status = 'running'"),
    )


def upgrade() -> None:
    summary_status.create(op.get_bind())
    op.drop_constraint("paper_summary_status", "paper_summaries", type_="check")
    change_status_type(summary_status, "status::paper_summary_status")


def downgrade() -> None:
    change_status_type(sa.Text(), "status::text")
    op.create_check_constraint(
        "paper_summary_status", "paper_summaries", "status IN ('queued', 'running', 'completed', 'failed')",
    )
    summary_status.drop(op.get_bind())
