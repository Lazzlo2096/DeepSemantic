"""Store the arXiv IDs found before checking for existing PDFs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_import_candidate_arxiv_ids"
down_revision = "0014_arxiv_catalog_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("candidate_arxiv_ids", postgresql.ARRAY(sa.Text())))


def downgrade() -> None:
    op.drop_column("arxiv_imports", "candidate_arxiv_ids")
