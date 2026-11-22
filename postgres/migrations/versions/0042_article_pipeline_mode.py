"""Add an independent switch for article indexing."""
from alembic import op


revision = "0042_article_pipeline_mode"
down_revision = "0041_rag_run_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE pipeline_settings
        ADD COLUMN article_mode text NOT NULL DEFAULT 'enabled'
            CHECK (article_mode IN ('enabled', 'deferred'))
    """)


def downgrade() -> None:
    op.drop_column("pipeline_settings", "article_mode")
