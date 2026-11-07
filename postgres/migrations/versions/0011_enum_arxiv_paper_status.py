"""Store arXiv paper processing statuses as a PostgreSQL enum."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_enum_arxiv_paper_status"
down_revision = "0010_create_arxiv_imports"
branch_labels = None
depends_on = None


paper_status = postgresql.ENUM("downloaded", "texted", "chunked", "embeddinged", name="arxiv_paper_status")


def replace_refresh_function(status_cast: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION refresh_arxiv_paper_status(paper_id text) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Concurrent embeddings for the same paper must see each other's committed chunk updates.
            PERFORM 1 FROM arxiv_papers WHERE arxiv_id = paper_id FOR NO KEY UPDATE;

            UPDATE arxiv_papers AS paper
            SET status = (CASE
                WHEN paper.text_object_key IS NULL THEN 'downloaded'
                WHEN NOT EXISTS (SELECT 1 FROM paper_chunks WHERE arxiv_id = paper_id) THEN 'texted'
                WHEN EXISTS (
                    SELECT 1 FROM paper_chunks AS link JOIN chunks ON chunks.id = link.chunk_id
                    WHERE link.arxiv_id = paper_id
                      AND (chunks.qdrant_collection IS NULL OR chunks.qdrant_point_id IS NULL)
                ) THEN 'chunked'
                ELSE 'embeddinged'
            END){status_cast}
            WHERE paper.arxiv_id = paper_id;
        END;
        $$
    """)


def upgrade() -> None:
    paper_status.create(op.get_bind())
    op.drop_constraint("arxiv_papers_status_check", "arxiv_papers", type_="check")
    op.alter_column("arxiv_papers", "status", server_default=None)
    op.alter_column("arxiv_papers", "status", type_=paper_status, postgresql_using="status::arxiv_paper_status")
    op.alter_column("arxiv_papers", "status", server_default="downloaded")
    replace_refresh_function("::arxiv_paper_status")


def downgrade() -> None:
    replace_refresh_function("")
    op.alter_column("arxiv_papers", "status", server_default=None)
    op.alter_column("arxiv_papers", "status", type_=sa.Text(), postgresql_using="status::text")
    op.alter_column("arxiv_papers", "status", server_default="downloaded")
    op.create_check_constraint(
        "arxiv_papers_status_check", "arxiv_papers", "status IN ('downloaded', 'texted', 'chunked', 'embeddinged')"
    )
    paper_status.drop(op.get_bind())
