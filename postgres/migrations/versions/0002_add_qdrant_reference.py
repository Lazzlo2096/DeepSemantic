"""Add Qdrant point references."""

from alembic import op
import sqlalchemy as sa


revision = "0002_add_qdrant_reference"
down_revision = "0001_create_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("qdrant_collection", sa.Text()), if_not_exists=True)
    op.add_column("documents", sa.Column("qdrant_point_id", sa.BigInteger()), if_not_exists=True)


def downgrade() -> None:
    op.drop_column("documents", "qdrant_point_id")
    op.drop_column("documents", "qdrant_collection")
