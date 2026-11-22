"""Store processing artifacts for user-uploaded Space documents."""

from alembic import op
import sqlalchemy as sa


revision = "0048_user_document_processing"
down_revision = "0047_move_neural_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_processing",
        sa.Column("space_file_id", sa.Uuid(), sa.ForeignKey("public.space_files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("text_object_key", sa.Text()),
        sa.Column("text_version", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_version", sa.Text()),
        sa.Column("summary_generation", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="neural_things",
    )
    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", sa.BigInteger(), sa.ForeignKey("neural_things.chunks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("space_file_id", sa.Uuid(), sa.ForeignKey("public.space_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("source_start", sa.Integer()),
        sa.Column("source_end", sa.Integer()),
        sa.Column("generation", sa.Text(), nullable=False),
        sa.CheckConstraint("source_kind IN ('article', 'summary')", name="document_chunks_source_kind_check"),
        sa.CheckConstraint("chunk_index >= 0", name="document_chunks_index_nonnegative"),
        sa.UniqueConstraint("space_file_id", "source_kind", "chunk_index", name="document_chunks_file_index_key"),
        schema="neural_things",
    )
    op.create_index("document_chunks_file_idx", "document_chunks", ["space_file_id", "source_kind"], schema="neural_things")


def downgrade() -> None:
    op.drop_index("document_chunks_file_idx", table_name="document_chunks", schema="neural_things")
    op.drop_table("document_chunks", schema="neural_things")
    op.drop_table("document_processing", schema="neural_things")
