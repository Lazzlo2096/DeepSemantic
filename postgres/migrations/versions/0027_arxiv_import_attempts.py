"""Store arXiv attempt progress in relational rows."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0027_arxiv_import_attempts"
down_revision = "0026_drop_arxiv_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arxiv_import_attempts",
        sa.Column("import_id", sa.Uuid(), sa.ForeignKey("arxiv_imports.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("source_format", sa.Text()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("processed_articles", sa.Integer()),
        sa.Column("articles_found", sa.BigInteger()),
        sa.Column("pdf_available", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pdf_reused", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pdf_failures", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("html_unavailable", sa.BigInteger()),
    )
    op.execute("""
        INSERT INTO arxiv_import_attempts (
            import_id, attempt, source_format, finished_at, processed_articles,
            articles_found, pdf_available, pdf_reused, pdf_failures, html_unavailable
        )
        SELECT id, attempts, checkpoint->>'source_format',
               CASE WHEN checkpoint IS NULL OR status IN ('completed', 'failed')
                    THEN COALESCE(finished_at, available_at) END,
               (checkpoint->>'next_index')::integer,
               (result->>'articles_found')::bigint,
               COALESCE((result->>'pdf_available')::bigint, 0),
               COALESCE((result->>'pdf_reused')::bigint, 0),
               COALESCE((result->>'pdf_failures')::bigint, 0),
               (result->>'html_unavailable')::bigint
        FROM arxiv_imports
        WHERE attempts > 0 OR checkpoint IS NOT NULL OR result IS NOT NULL
    """)
    op.execute("DROP FUNCTION arxiv_import_result(uuid)")
    op.execute("""
        CREATE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS TABLE (
            articles_found bigint, pdf_available bigint, pdf_reused bigint, pdf_failures bigint,
            html_unavailable bigint, processed_articles integer
        )
        LANGUAGE sql STABLE STRICT AS $$
            SELECT attempt.articles_found, attempt.pdf_available, attempt.pdf_reused, attempt.pdf_failures,
                   attempt.html_unavailable,
                   CASE WHEN attempt.attempt = job.attempts
                              AND (attempt.finished_at IS NULL OR job.status IN ('completed', 'failed'))
                        THEN attempt.processed_articles END
            FROM arxiv_imports AS job
            JOIN arxiv_import_attempts AS attempt ON attempt.import_id = job.id
            WHERE job.id = p_import_id AND attempt.articles_found IS NOT NULL
            ORDER BY attempt.attempt DESC
            LIMIT 1;
        $$
    """)
    op.drop_column("arxiv_imports", "checkpoint")
    op.drop_column("arxiv_imports", "result")


def downgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("checkpoint", postgresql.JSONB()))
    op.add_column("arxiv_imports", sa.Column("result", postgresql.JSONB()))
    op.execute("""
        UPDATE arxiv_imports AS job
        SET result = (
                SELECT jsonb_strip_nulls(to_jsonb(stats) - 'processed_articles')
                FROM arxiv_import_result(job.id) AS stats
            ),
            checkpoint = (
                SELECT jsonb_strip_nulls(jsonb_build_object(
                    'next_index', attempt.processed_articles, 'source_format', attempt.source_format
                ))
                FROM arxiv_import_attempts AS attempt
                WHERE attempt.import_id = job.id AND attempt.attempt = job.attempts
                  AND (attempt.finished_at IS NULL OR job.status IN ('completed', 'failed'))
            )
    """)
    op.execute("DROP FUNCTION arxiv_import_result(uuid)")
    op.execute("""
        CREATE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS TABLE (articles_found bigint, pdf_available bigint, pdf_reused bigint, pdf_failures bigint)
        LANGUAGE sql STABLE STRICT AS $$
            SELECT counters.*
            FROM arxiv_imports AS job
            CROSS JOIN LATERAL jsonb_to_record(job.result) AS counters(
                articles_found bigint, pdf_available bigint, pdf_reused bigint, pdf_failures bigint
            )
            WHERE job.id = p_import_id AND job.result IS NOT NULL;
        $$
    """)
    op.drop_table("arxiv_import_attempts")
