"""Expose the recorded counters for an arXiv import job."""

from alembic import op


revision = "0016_arxiv_import_result"
down_revision = "0015_import_candidate_arxiv_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION arxiv_import_result(p_import_id uuid)
        RETURNS jsonb
        LANGUAGE sql STABLE STRICT AS $$
            SELECT job.result
            FROM arxiv_imports AS job
            WHERE job.id = p_import_id;
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION arxiv_import_result(uuid)")
