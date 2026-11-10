"""Store character ranges in the text from which paper chunks were built."""

from alembic import op
import sqlalchemy as sa


revision = "0033_paper_chunk_offsets"
down_revision = "0032_rechunk_html_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_chunks", sa.Column("source_start", sa.Integer()))
    op.add_column("paper_chunks", sa.Column("source_end", sa.Integer()))
    op.create_check_constraint(
        "paper_chunks_source_range_check", "paper_chunks",
        "(source_start IS NULL AND source_end IS NULL) OR "
        "(source_start IS NOT NULL AND source_end IS NOT NULL AND source_start >= 0 AND source_end > source_start)",
    )


def downgrade() -> None:
    op.drop_constraint("paper_chunks_source_range_check", "paper_chunks", type_="check")
    op.drop_column("paper_chunks", "source_end")
    op.drop_column("paper_chunks", "source_start")
