"""Rename documents to chunks and link ordered chunks to arXiv papers."""

from alembic import op
import sqlalchemy as sa


revision = "0006_create_paper_chunks"
down_revision = "0005_add_arxiv_text_object_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("documents", "chunks")
    op.create_table(
        "paper_chunks",
        sa.Column("chunk_id", sa.BigInteger(), sa.ForeignKey("chunks.id"), primary_key=True, autoincrement=False),
        sa.Column("arxiv_id", sa.Text(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["arxiv_id", "bucket"], ["arxiv_downloads.arxiv_id", "arxiv_downloads.bucket"]),
        sa.UniqueConstraint("arxiv_id", "bucket", "chunk_index", name="paper_chunks_paper_index_key"),
        sa.CheckConstraint("chunk_index >= 0", name="paper_chunks_index_nonnegative"),
    )


def downgrade() -> None:
    op.drop_table("paper_chunks")
    op.rename_table("chunks", "documents")
