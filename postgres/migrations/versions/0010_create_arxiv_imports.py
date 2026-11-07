"""Create the arXiv import jobs table."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_create_arxiv_imports"
down_revision = "0009_add_paper_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arxiv_imports",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False, server_default=""),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("result", postgresql.JSONB()),
        sa.CheckConstraint("date_from <= date_to", name="arxiv_imports_date_order"),
        sa.CheckConstraint("attempts >= 0", name="arxiv_imports_attempts_nonnegative"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="arxiv_imports_status_check",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (lease_until IS NOT NULL)",
            name="arxiv_imports_running_lease_check",
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed')) = (finished_at IS NOT NULL)",
            name="arxiv_imports_finished_at_check",
        ),
    )
    op.create_index(
        "arxiv_imports_queued_idx",
        "arxiv_imports",
        ["available_at", "created_at", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "arxiv_imports_running_idx",
        "arxiv_imports",
        ["lease_until"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_table("arxiv_imports")
