"""Keep one catalog entry and one ordered chunk set per arXiv version."""

from alembic import op
import sqlalchemy as sa


revision = "0007_key_papers_by_arxiv_id"
down_revision = "0006_create_paper_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("paper_chunks_arxiv_id_bucket_fkey", "paper_chunks", type_="foreignkey")
    op.drop_constraint("arxiv_downloads_pkey", "arxiv_downloads", type_="primary")

    # Keep the newest available chunk set. Other chunk rows and their vectors remain usable by ID.
    op.execute("""
        WITH chosen AS (
            SELECT DISTINCT ON (paper.arxiv_id) paper.arxiv_id, paper.bucket
            FROM arxiv_downloads AS paper
            WHERE EXISTS (
                SELECT 1 FROM paper_chunks AS link
                WHERE (link.arxiv_id, link.bucket) = (paper.arxiv_id, paper.bucket)
            )
            ORDER BY paper.arxiv_id, paper.downloaded_at DESC, paper.bucket
        )
        DELETE FROM paper_chunks AS link USING chosen
        WHERE link.arxiv_id = chosen.arxiv_id AND link.bucket <> chosen.bucket
    """)
    op.execute("""
        WITH chosen AS (
            SELECT DISTINCT ON (arxiv_id) arxiv_id, bucket
            FROM arxiv_downloads
            ORDER BY arxiv_id, downloaded_at DESC, bucket
        )
        DELETE FROM arxiv_downloads AS paper USING chosen
        WHERE paper.arxiv_id = chosen.arxiv_id AND paper.bucket <> chosen.bucket
    """)

    op.create_primary_key("arxiv_downloads_pkey", "arxiv_downloads", ["arxiv_id"])
    op.drop_constraint("paper_chunks_paper_index_key", "paper_chunks", type_="unique")
    op.drop_column("paper_chunks", "bucket")
    op.create_foreign_key("paper_chunks_arxiv_id_fkey", "paper_chunks", "arxiv_downloads", ["arxiv_id"], ["arxiv_id"])
    op.create_unique_constraint("paper_chunks_paper_index_key", "paper_chunks", ["arxiv_id", "chunk_index"])


def downgrade() -> None:
    op.drop_constraint("paper_chunks_arxiv_id_fkey", "paper_chunks", type_="foreignkey")
    op.drop_constraint("paper_chunks_paper_index_key", "paper_chunks", type_="unique")
    op.drop_constraint("arxiv_downloads_pkey", "arxiv_downloads", type_="primary")
    op.create_primary_key("arxiv_downloads_pkey", "arxiv_downloads", ["arxiv_id", "bucket"])

    op.add_column("paper_chunks", sa.Column("bucket", sa.Text()))
    op.execute("""
        UPDATE paper_chunks AS link SET bucket = paper.bucket
        FROM arxiv_downloads AS paper WHERE link.arxiv_id = paper.arxiv_id
    """)
    op.alter_column("paper_chunks", "bucket", nullable=False)
    op.create_foreign_key(
        "paper_chunks_arxiv_id_bucket_fkey", "paper_chunks", "arxiv_downloads", ["arxiv_id", "bucket"], ["arxiv_id", "bucket"]
    )
    op.create_unique_constraint("paper_chunks_paper_index_key", "paper_chunks", ["arxiv_id", "bucket", "chunk_index"])
