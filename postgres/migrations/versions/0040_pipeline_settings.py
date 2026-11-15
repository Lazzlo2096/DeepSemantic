"""Persistent pipeline configuration; execution state stays in Temporal."""
from alembic import op

revision = "0040_pipeline_settings"
down_revision = "0039_temporal_native_state"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE pipeline_settings (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            summary_mode text NOT NULL DEFAULT 'enabled' CHECK (summary_mode IN ('enabled', 'deferred')),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO pipeline_settings (singleton) VALUES (true);
    """)


def downgrade():
    op.execute("DROP TABLE pipeline_settings")
