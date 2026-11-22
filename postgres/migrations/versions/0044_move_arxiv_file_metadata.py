"""Move arXiv object metadata to space_files."""

from alembic import op
import sqlalchemy as sa


revision = "0044_move_arxiv_file_metadata"
down_revision = "0043_create_spaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW current_paper_chunks; DROP VIEW paper_chunk_sources;")
    op.add_column("arxiv_papers", sa.Column("space_file_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "arxiv_papers_space_file_id_fkey",
        "arxiv_papers",
        "space_files",
        ["space_file_id"],
        ["id"],
    )
    op.create_index("arxiv_papers_space_file_id_idx", "arxiv_papers", ["space_file_id"])
    op.drop_column("arxiv_papers", "bucket")
    op.drop_column("arxiv_papers", "object_key")
    op.drop_column("arxiv_papers", "downloaded_at")
    op.execute("""
        CREATE VIEW paper_chunk_sources AS
        SELECT paper.arxiv_id, 'article'::text AS source_kind,
            jsonb_build_array(file.bucket, paper.text_object_key)::text AS source_version,
            file.bucket, paper.text_object_key
        FROM arxiv_papers paper JOIN space_files file ON file.id = paper.space_file_id
        WHERE paper.text_object_key IS NOT NULL
        UNION ALL
        SELECT result.arxiv_id, 'summary', md5(result.summary), NULL, NULL
        FROM paper_summaries result
        JOIN arxiv_papers paper USING (arxiv_id)
        JOIN space_files file ON file.id = paper.space_file_id
        WHERE result.source_version = jsonb_build_array(file.bucket, paper.text_object_key)::text;
        CREATE VIEW current_paper_chunks AS
        SELECT link.* FROM paper_chunks link JOIN paper_chunk_sources source USING (arxiv_id, source_kind)
        WHERE link.source_version IS NULL OR link.source_version = source.source_version;
    """)


def downgrade() -> None:
    op.add_column("arxiv_papers", sa.Column("bucket", sa.Text()))
    op.add_column("arxiv_papers", sa.Column("object_key", sa.Text()))
    op.add_column("arxiv_papers", sa.Column("downloaded_at", sa.DateTime(timezone=True)))
    op.execute("""
        UPDATE arxiv_papers AS paper
        SET bucket = file.bucket, object_key = file.object_key, downloaded_at = file.created_at
        FROM space_files AS file
        WHERE file.id = paper.space_file_id
    """)
    op.execute("DROP VIEW current_paper_chunks; DROP VIEW paper_chunk_sources;")
    op.drop_index("arxiv_papers_space_file_id_idx", table_name="arxiv_papers")
    op.drop_constraint("arxiv_papers_space_file_id_fkey", "arxiv_papers", type_="foreignkey")
    op.drop_column("arxiv_papers", "space_file_id")
    op.execute("""
        CREATE VIEW paper_chunk_sources AS
        SELECT arxiv_id, 'article'::text AS source_kind,
            jsonb_build_array(bucket, text_object_key)::text AS source_version,
            bucket, text_object_key
        FROM arxiv_papers WHERE text_object_key IS NOT NULL
        UNION ALL
        SELECT result.arxiv_id, 'summary', md5(result.summary), NULL, NULL
        FROM paper_summaries result JOIN arxiv_papers paper USING (arxiv_id)
        WHERE result.source_version = jsonb_build_array(paper.bucket, paper.text_object_key)::text;
        CREATE VIEW current_paper_chunks AS
        SELECT link.* FROM paper_chunks link JOIN paper_chunk_sources source USING (arxiv_id, source_kind)
        WHERE link.source_version IS NULL OR link.source_version = source.source_version;
    """)
