"""Remove stored arXiv import counters in favor of the catalog function."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_drop_arxiv_import_result"
down_revision = "0018_arxiv_import_result_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("arxiv_imports", "result")


def downgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("result", postgresql.JSONB()))
