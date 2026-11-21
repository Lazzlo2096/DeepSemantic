"""Store application accounts, sessions and invitations."""

from alembic import op
import sqlalchemy as sa


revision = "0001_auth_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema("auth"))
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="user", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("email <> '' AND email = btrim(email)", name="users_email_valid"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="users_role_valid"),
        schema="auth",
    )
    op.create_index("users_email_lower_key", "users", [sa.text("lower(email)")], unique=True, schema="auth")

    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="sessions_token_hash_length"),
        sa.CheckConstraint("expires_at > created_at", name="sessions_expiry_order"),
        schema="auth",
    )
    op.create_index("sessions_user_id_idx", "sessions", ["user_id"], schema="auth")

    op.create_table(
        "invites",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="user", nullable=False),
        sa.Column("invited_by", sa.Uuid(), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="invites_token_hash_length"),
        sa.CheckConstraint("email <> '' AND email = btrim(email)", name="invites_email_valid"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="invites_role_valid"),
        sa.CheckConstraint("expires_at > created_at", name="invites_expiry_order"),
        schema="auth",
    )


def downgrade() -> None:
    op.drop_table("invites", schema="auth")
    op.drop_table("sessions", schema="auth")
    op.drop_table("users", schema="auth")
    op.execute(sa.schema.DropSchema("auth"))
