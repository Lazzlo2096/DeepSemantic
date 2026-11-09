"""Remove stored snapshots of arXiv search results."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0026_drop_arxiv_candidates"
down_revision = "0025_store_arxiv_import_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE arxiv_imports
        SET checkpoint = COALESCE(checkpoint, '{}'::jsonb)
            || jsonb_build_object('source_format', candidate_articles->'source_format')
        WHERE status IN ('queued', 'running', 'pausing', 'paused')
          AND candidate_articles->>'source_format' IS NOT NULL
    """)
    op.drop_column("arxiv_imports", "candidate_articles")


def downgrade() -> None:
    op.add_column("arxiv_imports", sa.Column("candidate_articles", postgresql.JSONB()))
    # Without a snapshot, the old worker must start from the beginning of its new search.
    op.execute("""
        UPDATE arxiv_imports SET checkpoint = '{}'::jsonb
        WHERE status IN ('queued', 'running', 'pausing', 'paused') AND checkpoint IS NOT NULL
    """)
