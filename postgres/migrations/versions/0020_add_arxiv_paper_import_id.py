"""Link cataloged arXiv PDFs to their import jobs."""

from alembic import op
import sqlalchemy as sa


revision = "0020_add_arxiv_paper_import_id"
down_revision = "0019_drop_arxiv_import_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arxiv_papers", sa.Column("arxiv_import_id", sa.Uuid()))
    op.create_foreign_key(
        "arxiv_papers_arxiv_import_id_fkey", "arxiv_papers", "arxiv_imports",
        ["arxiv_import_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("arxiv_papers_arxiv_import_id_idx", "arxiv_papers", ["arxiv_import_id"])


def downgrade() -> None:
    op.drop_index("arxiv_papers_arxiv_import_id_idx", table_name="arxiv_papers")
    op.drop_constraint("arxiv_papers_arxiv_import_id_fkey", "arxiv_papers", type_="foreignkey")
    op.drop_column("arxiv_papers", "arxiv_import_id")
