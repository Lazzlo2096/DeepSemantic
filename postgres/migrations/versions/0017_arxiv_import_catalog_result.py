"""Calculate arXiv import counters from the candidate IDs and current catalog."""

from alembic import op


revision = "0017_arxiv_import_catalog_result"
down_revision = "0016_arxiv_import_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT AS $$
            SELECT counters.result
            FROM arxiv_imports AS job
            CROSS JOIN LATERAL (
                SELECT jsonb_build_object(
                    'articles_found', count(*),
                    'pdf_available', count(paper.arxiv_id),
                    'pdf_reused', count(*) FILTER (WHERE paper.downloaded_at < job.started_at),
                    'pdf_failures', count(*) FILTER (WHERE paper.arxiv_id IS NULL),
                    'text_failures', count(*) FILTER (
                        WHERE paper.arxiv_id IS NOT NULL AND paper.text_object_key IS NULL
                    ),
                    'chunk_failures', count(*) FILTER (WHERE paper.status = 'texted')
                ) AS result
                FROM unnest(job.candidate_arxiv_ids) AS candidate(arxiv_id)
                LEFT JOIN arxiv_papers AS paper
                    ON paper.arxiv_id = candidate.arxiv_id AND paper.bucket = job.bucket
            ) AS counters
            WHERE job.id = p_import_id AND job.candidate_arxiv_ids IS NOT NULL;
        $$
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT AS $$
            SELECT job.result
            FROM arxiv_imports AS job
            WHERE job.id = p_import_id;
        $$
    """)
