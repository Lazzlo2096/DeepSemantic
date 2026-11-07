"""Create the arXiv PDF download catalog."""

from alembic import op
import sqlalchemy as sa


revision = "0003_create_arxiv_downloads"
down_revision = "0002_add_qdrant_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arxiv_downloads",
        sa.Column("arxiv_id", sa.Text(), primary_key=True),
        sa.Column("bucket", sa.Text(), primary_key=True),
        sa.Column("date_from", sa.Date(), primary_key=True),
        sa.Column("date_to", sa.Date(), primary_key=True),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authors", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("date_from <= date_to", name="arxiv_downloads_date_order"),
    )


def downgrade() -> None:
    op.drop_table("arxiv_downloads")
