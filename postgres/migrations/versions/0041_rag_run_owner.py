"""Link RAG runs to accounts managed by the auth migration history."""

from alembic import op
import sqlalchemy as sa


revision = "0041_rag_run_owner"
down_revision = "0040_pipeline_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rag_runs", sa.Column("user_id", sa.Uuid()))
    op.create_foreign_key(
        "rag_runs_user_id_fkey", "rag_runs", "users", ["user_id"], ["id"], referent_schema="auth",
    )
    op.create_index("rag_runs_user_id_idx", "rag_runs", ["user_id"])


def downgrade() -> None:
    op.drop_index("rag_runs_user_id_idx", table_name="rag_runs")
    op.drop_constraint("rag_runs_user_id_fkey", "rag_runs", type_="foreignkey")
    op.drop_column("rag_runs", "user_id")
