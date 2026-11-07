"""Track the last completed stage for each arXiv paper."""

from alembic import op
import sqlalchemy as sa


revision = "0009_add_paper_status"
down_revision = "0008_rename_arxiv_papers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arxiv_papers", sa.Column("status", sa.Text(), nullable=False, server_default="downloaded"))
    op.create_check_constraint(
        "arxiv_papers_status_check", "arxiv_papers", "status IN ('downloaded', 'texted', 'chunked', 'embeddinged')"
    )
    op.execute("""
        CREATE FUNCTION refresh_arxiv_paper_status(paper_id text) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Concurrent embeddings for the same paper must see each other's committed chunk updates.
            PERFORM 1 FROM arxiv_papers WHERE arxiv_id = paper_id FOR NO KEY UPDATE;

            UPDATE arxiv_papers AS paper
            SET status = CASE
                WHEN paper.text_object_key IS NULL THEN 'downloaded'
                WHEN NOT EXISTS (SELECT 1 FROM paper_chunks WHERE arxiv_id = paper_id) THEN 'texted'
                WHEN EXISTS (
                    SELECT 1 FROM paper_chunks AS link JOIN chunks ON chunks.id = link.chunk_id
                    WHERE link.arxiv_id = paper_id
                      AND (chunks.qdrant_collection IS NULL OR chunks.qdrant_point_id IS NULL)
                ) THEN 'chunked'
                ELSE 'embeddinged'
            END
            WHERE paper.arxiv_id = paper_id;
        END;
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION refresh_arxiv_paper_status(text)")
    op.drop_constraint("arxiv_papers_status_check", "arxiv_papers", type_="check")
    op.drop_column("arxiv_papers", "status")
