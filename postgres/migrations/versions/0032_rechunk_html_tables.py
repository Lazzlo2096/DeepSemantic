"""Refresh sources with HTML tables in their indexed chunks."""

from alembic import op


revision = "0032_rechunk_html_tables"
down_revision = "0031_paper_chunk_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE paper_chunks AS first
        SET source_version = NULL
        WHERE first.chunk_index = 0 AND (first.arxiv_id, first.source_kind) IN (
            SELECT link.arxiv_id, link.source_kind
            FROM paper_chunks AS link
            JOIN chunks AS chunk ON chunk.id = link.chunk_id
            WHERE chunk.text ~* '</?(table|thead|tbody|tfoot|caption|tr|th|td)([[:space:]]|>)'
        )
    """)


def downgrade() -> None:
    pass
