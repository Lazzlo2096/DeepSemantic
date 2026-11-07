"""Rename the arXiv catalog to arxiv_papers."""

from alembic import op


revision = "0008_rename_arxiv_papers"
down_revision = "0007_key_papers_by_arxiv_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("arxiv_downloads", "arxiv_papers")
    op.execute("ALTER TABLE arxiv_papers RENAME CONSTRAINT arxiv_downloads_pkey TO arxiv_papers_pkey")


def downgrade() -> None:
    op.execute("ALTER TABLE arxiv_papers RENAME CONSTRAINT arxiv_papers_pkey TO arxiv_downloads_pkey")
    op.rename_table("arxiv_papers", "arxiv_downloads")
