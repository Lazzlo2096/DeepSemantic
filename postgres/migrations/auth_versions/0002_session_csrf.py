"""Bind browser sessions to a CSRF token."""

from alembic import op
import sqlalchemy as sa


revision = "0002_session_csrf"
down_revision = "0001_auth_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("csrf_token_hash", sa.LargeBinary()), schema="auth")
    op.create_check_constraint(
        "sessions_csrf_token_hash_length",
        "sessions",
        "csrf_token_hash IS NULL OR octet_length(csrf_token_hash) = 32",
        schema="auth",
    )


def downgrade() -> None:
    op.drop_constraint("sessions_csrf_token_hash_length", "sessions", schema="auth", type_="check")
    op.drop_column("sessions", "csrf_token_hash", schema="auth")
