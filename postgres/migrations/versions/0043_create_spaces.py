"""Create user-owned spaces, access grants and uploaded files."""

from alembic import op
import sqlalchemy as sa


revision = "0043_create_spaces"
down_revision = "0042_article_pipeline_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spaces",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("visibility", sa.Text(), server_default="private", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["auth.users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("name <> '' AND name = btrim(name)", name="spaces_name_valid"),
        sa.CheckConstraint("visibility IN ('private', 'public')", name="spaces_visibility_valid"),
    )
    op.create_index("spaces_owner_id_idx", "spaces", ["owner_id"])
    op.create_index(
        "spaces_public_idx", "spaces", ["created_at"],
        postgresql_where=sa.text("visibility = 'public'"),
    )

    op.create_table(
        "space_members",
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("permission", sa.Text(), server_default="read", nullable=False),
        sa.Column("invited_by", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["auth.users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["auth.users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("space_id", "user_id"),
        sa.CheckConstraint("permission IN ('read', 'write')", name="space_members_permission_valid"),
    )
    op.create_index("space_members_user_id_idx", "space_members", ["user_id"])

    op.create_table(
        "space_files",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text()),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["auth.users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="space_files_size_valid"),
        sa.UniqueConstraint("bucket", "object_key", name="space_files_object_key_key"),
    )
    op.create_index("space_files_space_id_idx", "space_files", ["space_id", "created_at"])


def downgrade() -> None:
    op.drop_index("space_files_space_id_idx", table_name="space_files")
    op.drop_table("space_files")
    op.drop_index("space_members_user_id_idx", table_name="space_members")
    op.drop_table("space_members")
    op.drop_index("spaces_public_idx", table_name="spaces")
    op.drop_index("spaces_owner_id_idx", table_name="spaces")
    op.drop_table("spaces")
