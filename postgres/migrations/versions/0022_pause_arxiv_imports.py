"""Store pause state and document checkpoints for arXiv imports."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0022_pause_arxiv_imports"
down_revision = "0021_drop_arxiv_import_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("claim_id", sa.Uuid()))
    op.add_column("arxiv_imports", sa.Column("candidate_articles", postgresql.JSONB()))
    op.add_column("arxiv_imports", sa.Column("checkpoint", postgresql.JSONB()))
    op.drop_constraint("arxiv_imports_status_check", "arxiv_imports", type_="check")
    op.drop_constraint("arxiv_imports_running_lease_check", "arxiv_imports", type_="check")
    op.create_check_constraint(
        "arxiv_imports_status_check", "arxiv_imports",
        "status IN ('queued', 'running', 'pausing', 'paused', 'completed', 'failed')",
    )
    op.create_check_constraint(
        "arxiv_imports_running_lease_check", "arxiv_imports",
        "(status IN ('running', 'pausing')) = (lease_until IS NOT NULL)",
    )
    op.drop_index("arxiv_imports_running_idx", table_name="arxiv_imports")
    op.create_index(
        "arxiv_imports_running_idx", "arxiv_imports", ["lease_until"],
        postgresql_where=sa.text("status IN ('running', 'pausing')"),
    )


def downgrade() -> None:
    op.execute("""
        UPDATE arxiv_imports SET status = 'queued', lease_until = NULL, attempts = LEAST(attempts, 2)
        WHERE status IN ('pausing', 'paused') OR (status = 'queued' AND checkpoint IS NOT NULL)
    """)
    op.drop_index("arxiv_imports_running_idx", table_name="arxiv_imports")
    op.create_index(
        "arxiv_imports_running_idx", "arxiv_imports", ["lease_until"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_constraint("arxiv_imports_running_lease_check", "arxiv_imports", type_="check")
    op.drop_constraint("arxiv_imports_status_check", "arxiv_imports", type_="check")
    op.create_check_constraint(
        "arxiv_imports_running_lease_check", "arxiv_imports", "(status = 'running') = (lease_until IS NOT NULL)",
    )
    op.create_check_constraint(
        "arxiv_imports_status_check", "arxiv_imports", "status IN ('queued', 'running', 'completed', 'failed')",
    )
    op.drop_column("arxiv_imports", "checkpoint")
    op.drop_column("arxiv_imports", "candidate_articles")
    op.drop_column("arxiv_imports", "claim_id")
