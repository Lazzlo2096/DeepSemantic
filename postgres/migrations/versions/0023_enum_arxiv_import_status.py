"""Store arXiv import statuses as a PostgreSQL enum."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0023_enum_arxiv_import_status"
down_revision = "0022_pause_arxiv_imports"
branch_labels = None
depends_on = None


import_status = postgresql.ENUM(
    "queued", "running", "pausing", "paused", "completed", "failed", name="arxiv_import_status",
)


def change_status_type(column_type: sa.types.TypeEngine, cast: str) -> None:
    op.drop_index("arxiv_imports_queued_idx", table_name="arxiv_imports")
    op.drop_index("arxiv_imports_running_idx", table_name="arxiv_imports")
    op.drop_constraint("arxiv_imports_running_lease_check", "arxiv_imports", type_="check")
    op.drop_constraint("arxiv_imports_finished_at_check", "arxiv_imports", type_="check")

    op.alter_column("arxiv_imports", "status", server_default=None)
    op.alter_column("arxiv_imports", "status", type_=column_type, postgresql_using=cast)
    op.alter_column("arxiv_imports", "status", server_default="queued")

    op.create_check_constraint(
        "arxiv_imports_running_lease_check", "arxiv_imports",
        "(status IN ('running', 'pausing')) = (lease_until IS NOT NULL)",
    )
    op.create_check_constraint(
        "arxiv_imports_finished_at_check", "arxiv_imports",
        "(status IN ('completed', 'failed')) = (finished_at IS NOT NULL)",
    )
    op.create_index(
        "arxiv_imports_queued_idx", "arxiv_imports", ["available_at", "created_at", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "arxiv_imports_running_idx", "arxiv_imports", ["lease_until"],
        postgresql_where=sa.text("status IN ('running', 'pausing')"),
    )


def upgrade() -> None:
    import_status.create(op.get_bind())
    op.drop_constraint("arxiv_imports_status_check", "arxiv_imports", type_="check")
    change_status_type(import_status, "status::arxiv_import_status")


def downgrade() -> None:
    change_status_type(sa.Text(), "status::text")
    op.create_check_constraint(
        "arxiv_imports_status_check", "arxiv_imports",
        "status IN ('queued', 'running', 'pausing', 'paused', 'completed', 'failed')",
    )
    import_status.drop(op.get_bind())
