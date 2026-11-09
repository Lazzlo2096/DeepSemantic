"""Drop the stored error from arXiv import jobs."""

from alembic import op
import sqlalchemy as sa


revision = "0021_drop_arxiv_import_error"
down_revision = "0020_add_arxiv_paper_import_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("arxiv_imports", "last_error")


def downgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("last_error", sa.Text()))
