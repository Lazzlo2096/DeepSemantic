"""Index literal text search over stored chunks."""

from alembic import op


revision = "0034_index_chunk_text"
down_revision = "0033_paper_chunk_offsets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX chunks_text_trgm_idx ON chunks USING gin (text gin_trgm_ops)")


def downgrade() -> None:
    op.drop_index("chunks_text_trgm_idx", table_name="chunks")
