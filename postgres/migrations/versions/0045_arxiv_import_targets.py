"""Store arXiv import destinations selected by administrators."""

from alembic import op
import sqlalchemy as sa


revision = "0045_arxiv_import_targets"
down_revision = "0044_move_arxiv_file_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arxiv_import_targets",
        sa.Column("space_id", sa.Uuid(), primary_key=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["auth.users.id"], ondelete="RESTRICT"),
    )
    op.add_column("arxiv_imports", sa.Column("space_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "arxiv_imports_space_id_fkey", "arxiv_imports", "spaces", ["space_id"], ["id"],
    )
    op.create_index("arxiv_imports_space_id_idx", "arxiv_imports", ["space_id"])


def downgrade() -> None:
    op.drop_index("arxiv_imports_space_id_idx", table_name="arxiv_imports")
    op.drop_constraint("arxiv_imports_space_id_fkey", "arxiv_imports", type_="foreignkey")
    op.drop_column("arxiv_imports", "space_id")
    op.drop_table("arxiv_import_targets")
