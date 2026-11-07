"""Link catalog entries to the extracted text in RustFS."""

from alembic import op
import sqlalchemy as sa


revision = "0005_add_arxiv_text_object_key"
down_revision = "0004_drop_arxiv_date_range"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arxiv_downloads", sa.Column("text_object_key", sa.Text()))


def downgrade() -> None:
    op.drop_column("arxiv_downloads", "text_object_key")
