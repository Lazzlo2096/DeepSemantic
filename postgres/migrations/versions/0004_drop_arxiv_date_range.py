"""Keep one catalog row per article version and bucket."""

from alembic import op
import sqlalchemy as sa


revision = "0004_drop_arxiv_date_range"
down_revision = "0003_create_arxiv_downloads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("arxiv_downloads_pkey", "arxiv_downloads", type_="primary")
    op.drop_constraint("arxiv_downloads_date_order", "arxiv_downloads", type_="check")
    op.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (arxiv_id, bucket) arxiv_id, bucket, date_from, date_to
            FROM arxiv_downloads
            ORDER BY arxiv_id, bucket, downloaded_at DESC, date_from DESC, date_to DESC
        )
        DELETE FROM arxiv_downloads AS download
        USING latest
        WHERE (download.arxiv_id, download.bucket) = (latest.arxiv_id, latest.bucket)
          AND (download.date_from, download.date_to) <> (latest.date_from, latest.date_to)
    """)
    op.drop_column("arxiv_downloads", "date_from")
    op.drop_column("arxiv_downloads", "date_to")
    op.create_primary_key("arxiv_downloads_pkey", "arxiv_downloads", ["arxiv_id", "bucket"])


def downgrade() -> None:
    op.add_column("arxiv_downloads", sa.Column("date_from", sa.Date()))
    op.add_column("arxiv_downloads", sa.Column("date_to", sa.Date()))
    # Reconstruct a one-day range from the submission date after the original range is removed.
    op.execute("""
        UPDATE arxiv_downloads
        SET date_from = (submitted_at AT TIME ZONE 'UTC')::date,
            date_to = (submitted_at AT TIME ZONE 'UTC')::date
    """)
    op.alter_column("arxiv_downloads", "date_from", nullable=False)
    op.alter_column("arxiv_downloads", "date_to", nullable=False)
    op.drop_constraint("arxiv_downloads_pkey", "arxiv_downloads", type_="primary")
    op.create_primary_key(
        "arxiv_downloads_pkey", "arxiv_downloads", ["arxiv_id", "bucket", "date_from", "date_to"]
    )
    op.create_check_constraint("arxiv_downloads_date_order", "arxiv_downloads", "date_from <= date_to")
