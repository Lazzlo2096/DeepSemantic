"""Return result-style counters from the current arXiv catalog."""

from alembic import op


revision = "0014_arxiv_catalog_result"
down_revision = "0013_create_paper_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION arxiv_catalog_result(p_date_from date, p_date_to date, p_bucket text)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT AS $$
            SELECT jsonb_build_object('pdf_available', count(*))
            FROM arxiv_papers AS paper
            WHERE paper.bucket = p_bucket
              AND paper.submitted_at >= (p_date_from::timestamp AT TIME ZONE 'UTC')
              AND paper.submitted_at < ((p_date_to + 1)::timestamp AT TIME ZONE 'UTC');
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION arxiv_catalog_result(date, date, text)")
