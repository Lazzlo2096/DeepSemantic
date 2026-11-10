"""Track ordered chunks from article text and summaries separately."""

from alembic import op
import sqlalchemy as sa


revision = "0031_paper_chunk_sources"
down_revision = "0030_paper_summary_markdown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_chunks", sa.Column("source_kind", sa.Text(), nullable=False, server_default="article"))
    op.add_column("paper_chunks", sa.Column("source_version", sa.Text()))
    op.create_check_constraint(
        "paper_chunks_source_kind_check", "paper_chunks", "source_kind IN ('article', 'summary')",
    )
    op.drop_constraint("paper_chunks_paper_index_key", "paper_chunks", type_="unique")
    op.create_unique_constraint(
        "paper_chunks_paper_index_key", "paper_chunks", ["arxiv_id", "source_kind", "chunk_index"],
    )
    op.execute("""
        CREATE VIEW paper_chunk_sources AS
        SELECT arxiv_id, 'article'::text AS source_kind,
               jsonb_build_array(bucket, text_object_key)::text AS source_version, bucket, text_object_key
        FROM arxiv_papers
        WHERE status = 'texted' AND text_object_key IS NOT NULL
        UNION ALL
        SELECT arxiv_id, 'summary', md5(summary), NULL, NULL
        FROM paper_summaries
        WHERE status = 'completed'
    """)
    op.execute("""
        CREATE VIEW current_paper_chunks AS
        SELECT link.*
        FROM paper_chunks AS link
        JOIN paper_chunk_sources AS source USING (arxiv_id, source_kind)
        WHERE link.source_version IS NULL OR link.source_version = source.source_version
    """)


def downgrade() -> None:
    op.execute("DROP VIEW current_paper_chunks")
    op.execute("DROP VIEW paper_chunk_sources")
    op.execute("""
        WITH removed AS (DELETE FROM paper_chunks WHERE source_kind = 'summary' RETURNING chunk_id)
        DELETE FROM chunks USING removed WHERE chunks.id = removed.chunk_id
    """)
    op.drop_constraint("paper_chunks_paper_index_key", "paper_chunks", type_="unique")
    op.create_unique_constraint("paper_chunks_paper_index_key", "paper_chunks", ["arxiv_id", "chunk_index"])
    op.drop_constraint("paper_chunks_source_kind_check", "paper_chunks", type_="check")
    op.drop_column("paper_chunks", "source_version")
    op.drop_column("paper_chunks", "source_kind")
