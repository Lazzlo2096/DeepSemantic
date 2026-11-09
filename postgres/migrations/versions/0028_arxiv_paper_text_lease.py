"""Track text extraction leases separately from retry delays."""

from alembic import op
import sqlalchemy as sa


revision = "0028_arxiv_paper_text_lease"
down_revision = "0027_arxiv_import_attempts"
branch_labels = None
depends_on = None


QUEUE_INDEX = "arxiv_papers_text_queue_idx"
LEASE_CHECK = "arxiv_papers_text_lease_check"
QUEUE_FILTER = "status IN ('downloaded', 'texting') AND lower(right(object_key, 5)) = '.html'"


def upgrade() -> None:
    op.add_column("arxiv_papers", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.execute("UPDATE arxiv_papers SET lease_until = text_available_at WHERE status = 'texting'")
    op.create_check_constraint(LEASE_CHECK, "arxiv_papers", "(status = 'texting') = (lease_until IS NOT NULL)")
    op.drop_index(QUEUE_INDEX, table_name="arxiv_papers")
    op.create_index(
        QUEUE_INDEX, "arxiv_papers", [sa.text("COALESCE(lease_until, text_available_at)"), "arxiv_id"],
        postgresql_where=sa.text(QUEUE_FILTER),
    )


def downgrade() -> None:
    op.drop_index(QUEUE_INDEX, table_name="arxiv_papers")
    op.drop_constraint(LEASE_CHECK, "arxiv_papers", type_="check")
    op.execute("UPDATE arxiv_papers SET text_available_at = lease_until WHERE status = 'texting'")
    op.drop_column("arxiv_papers", "lease_until")
    op.create_index(
        QUEUE_INDEX, "arxiv_papers", ["text_available_at", "arxiv_id"],
        postgresql_where=sa.text(QUEUE_FILTER),
    )
