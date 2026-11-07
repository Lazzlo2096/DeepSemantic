"""Return committed status transitions to callers for structured logging."""

from alembic import op


revision = "0012_report_paper_status_change"
down_revision = "0011_enum_arxiv_paper_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute("DROP FUNCTION refresh_arxiv_paper_status_change(text)")
