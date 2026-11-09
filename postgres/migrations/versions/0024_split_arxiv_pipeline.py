"""Separate arXiv downloading, text extraction, and summarization."""

from alembic import op
import sqlalchemy as sa


revision = "0024_split_arxiv_pipeline"
down_revision = "0023_enum_arxiv_import_status"
branch_labels = None
depends_on = None


def replace_paper_status_type(labels: str, status_expression: str) -> None:
    op.alter_column("arxiv_papers", "status", server_default=None)
    op.execute("ALTER TYPE arxiv_paper_status RENAME TO old_arxiv_paper_status")
    op.execute(f"CREATE TYPE arxiv_paper_status AS ENUM ({labels})")
    op.execute(f"""
        ALTER TABLE arxiv_papers ALTER COLUMN status TYPE arxiv_paper_status
        USING ({status_expression})::arxiv_paper_status
    """)
    op.execute("DROP TYPE old_arxiv_paper_status")
    op.alter_column("arxiv_papers", "status", server_default="downloaded")


def replace_import_result(*, legacy: bool = False) -> None:
    extra_columns = ", text_failures bigint, chunk_failures bigint" if legacy else ""
    extra_counts = """
        , count(*) FILTER (WHERE paper.arxiv_id IS NOT NULL AND paper.text_object_key IS NULL) AS text_failures
        , count(*) FILTER (WHERE paper.status = 'texted') AS chunk_failures
    """ if legacy else ""
    op.execute("DROP FUNCTION arxiv_import_result(uuid)")
    op.execute(f"""
        CREATE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS TABLE (
            articles_found bigint, pdf_available bigint, pdf_reused bigint, pdf_failures bigint{extra_columns}
        )
        LANGUAGE sql STABLE STRICT AS $$
            SELECT counters.*
            FROM arxiv_imports AS job
            CROSS JOIN LATERAL (
                SELECT count(*) AS articles_found,
                       count(paper.arxiv_id) AS pdf_available,
                       count(*) FILTER (WHERE paper.downloaded_at < job.started_at) AS pdf_reused,
                       count(*) FILTER (WHERE paper.arxiv_id IS NULL) AS pdf_failures
                       {extra_counts}
                FROM unnest(job.candidate_arxiv_ids) AS candidate(arxiv_id)
                LEFT JOIN arxiv_papers AS paper
                    ON paper.arxiv_id = candidate.arxiv_id AND paper.bucket = job.bucket
            ) AS counters
            WHERE job.id = p_import_id AND job.candidate_arxiv_ids IS NOT NULL;
        $$
    """)


def upgrade() -> None:
    op.execute("DROP FUNCTION refresh_arxiv_paper_status_change(text)")
    op.execute("DROP FUNCTION refresh_arxiv_paper_status(text)")
    replace_import_result()
    replace_paper_status_type(
        "'downloaded', 'texting', 'texted'",
        "CASE WHEN text_object_key IS NULL THEN 'downloaded' ELSE 'texted' END",
    )
    op.add_column("arxiv_papers", sa.Column(
        "text_available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
    ))
    op.create_index(
        "arxiv_papers_text_queue_idx", "arxiv_papers", ["text_available_at", "arxiv_id"],
        postgresql_where=sa.text("status IN ('downloaded', 'texting') AND lower(right(object_key, 5)) = '.html'"),
    )


def downgrade() -> None:
    op.drop_index("arxiv_papers_text_queue_idx", table_name="arxiv_papers")
    op.drop_column("arxiv_papers", "text_available_at")
    replace_paper_status_type(
        "'downloaded', 'texted', 'chunked', 'embeddinged'",
        "CASE WHEN status = 'texting' THEN 'downloaded' ELSE status::text END",
    )
    op.execute("""
        UPDATE arxiv_papers AS paper SET status = (CASE
            WHEN EXISTS (
                SELECT 1 FROM paper_chunks AS link JOIN chunks ON chunks.id = link.chunk_id
                WHERE link.arxiv_id = paper.arxiv_id
                  AND (chunks.qdrant_collection IS NULL OR chunks.qdrant_point_id IS NULL)
            ) THEN 'chunked' ELSE 'embeddinged'
        END)::arxiv_paper_status
        WHERE paper.status = 'texted'
          AND EXISTS (SELECT 1 FROM paper_chunks AS link WHERE link.arxiv_id = paper.arxiv_id)
    """)
    op.execute("""
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
            END)::arxiv_paper_status
            WHERE paper.arxiv_id = paper_id;
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION refresh_arxiv_paper_status_change(paper_id text)
        RETURNS TABLE (old_status arxiv_paper_status, new_status arxiv_paper_status)
        LANGUAGE plpgsql AS $$
        BEGIN
            SELECT status INTO old_status FROM arxiv_papers
            WHERE arxiv_id = paper_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RETURN;
            END IF;

            PERFORM refresh_arxiv_paper_status(paper_id);
            SELECT status INTO new_status FROM arxiv_papers WHERE arxiv_id = paper_id;
            IF old_status IS DISTINCT FROM new_status THEN
                RETURN NEXT;
            END IF;
        END;
        $$
    """)
    replace_import_result(legacy=True)
