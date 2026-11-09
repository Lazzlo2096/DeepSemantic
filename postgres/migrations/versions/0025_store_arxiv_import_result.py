"""Store import counters and remove the redundant candidate ID array."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0025_store_arxiv_import_result"
down_revision = "0024_split_arxiv_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("result", postgresql.JSONB()))
    op.execute("""
        UPDATE arxiv_imports AS job
        SET result = CASE WHEN job.candidate_articles IS NOT NULL THEN jsonb_build_object(
            'articles_found', jsonb_array_length(job.candidate_articles->'articles'),
            'pdf_available', COALESCE((job.checkpoint #>> '{counters,pdf_available}')::bigint, 0),
            'pdf_reused', COALESCE((job.checkpoint #>> '{counters,pdf_reused}')::bigint, 0),
            'pdf_failures', COALESCE((job.checkpoint #>> '{counters,pdf_failures}')::bigint, 0)
        ) ELSE (
            SELECT to_jsonb(counters) FROM arxiv_import_result(job.id) AS counters
        ) END
        WHERE job.candidate_articles IS NOT NULL OR job.candidate_arxiv_ids IS NOT NULL
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION arxiv_import_result(p_import_id uuid)
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
    op.drop_column("arxiv_imports", "candidate_arxiv_ids")


def downgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("candidate_arxiv_ids", postgresql.ARRAY(sa.Text())))
    # Finished jobs retain counts only, so only snapshots and known-empty lists can be restored.
    op.execute("""
        UPDATE arxiv_imports AS job
        SET candidate_arxiv_ids = ARRAY(
            SELECT candidate.article->>'arxiv_id'
            FROM jsonb_array_elements(job.candidate_articles->'articles') WITH ORDINALITY AS candidate(article, ord)
            ORDER BY candidate.ord
        )
        WHERE job.candidate_articles IS NOT NULL OR (job.result->>'articles_found')::bigint = 0
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS TABLE (articles_found bigint, pdf_available bigint, pdf_reused bigint, pdf_failures bigint)
        LANGUAGE sql STABLE STRICT AS $$
            SELECT counters.*
            FROM arxiv_imports AS job
            CROSS JOIN LATERAL (
                SELECT count(*) AS articles_found,
                       count(paper.arxiv_id) AS pdf_available,
                       count(*) FILTER (WHERE paper.downloaded_at < job.started_at) AS pdf_reused,
                       count(*) FILTER (WHERE paper.arxiv_id IS NULL) AS pdf_failures
                FROM unnest(job.candidate_arxiv_ids) AS candidate(arxiv_id)
                LEFT JOIN arxiv_papers AS paper
                    ON paper.arxiv_id = candidate.arxiv_id AND paper.bucket = job.bucket
            ) AS counters
            WHERE job.id = p_import_id AND job.candidate_arxiv_ids IS NOT NULL;
        $$
    """)
    op.drop_column("arxiv_imports", "result")
