"""Return arXiv import counters as SQL columns."""

from alembic import op


revision = "0018_arxiv_import_result_table"
down_revision = "0017_arxiv_import_catalog_result"
branch_labels = None
depends_on = None


COUNTERS_QUERY = """
    SELECT counters.*
    FROM arxiv_imports AS job
    CROSS JOIN LATERAL (
        SELECT count(*) AS articles_found,
               count(paper.arxiv_id) AS pdf_available,
               count(*) FILTER (WHERE paper.downloaded_at < job.started_at) AS pdf_reused,
               count(*) FILTER (WHERE paper.arxiv_id IS NULL) AS pdf_failures,
               count(*) FILTER (
                   WHERE paper.arxiv_id IS NOT NULL AND paper.text_object_key IS NULL
               ) AS text_failures,
               count(*) FILTER (WHERE paper.status = 'texted') AS chunk_failures
        FROM unnest(job.candidate_arxiv_ids) AS candidate(arxiv_id)
        LEFT JOIN arxiv_papers AS paper
            ON paper.arxiv_id = candidate.arxiv_id AND paper.bucket = job.bucket
    ) AS counters
    WHERE job.id = p_import_id AND job.candidate_arxiv_ids IS NOT NULL
"""


def upgrade() -> None:
    op.execute("DROP FUNCTION arxiv_import_result(uuid)")
    op.execute(f"""
        CREATE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS TABLE (
            articles_found bigint,
            pdf_available bigint,
            pdf_reused bigint,
            pdf_failures bigint,
            text_failures bigint,
            chunk_failures bigint
        )
        LANGUAGE sql STABLE STRICT AS $$
            {COUNTERS_QUERY};
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION arxiv_import_result(uuid)")
    op.execute(f"""
        CREATE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT AS $$
            SELECT to_jsonb(counters) FROM ({COUNTERS_QUERY}) AS counters;
        $$
    """)
