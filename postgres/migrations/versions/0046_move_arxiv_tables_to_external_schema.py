"""Move arXiv integration tables into their own schema."""

from alembic import op


revision = "0046_move_arxiv_schema"
down_revision = "0045_arxiv_import_targets"
branch_labels = None
depends_on = None


SCHEMA = "external_integration_arxiv"
TABLES = ("arxiv_import_targets", "arxiv_imports", "arxiv_papers")


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    for table in TABLES:
        op.execute(f'ALTER TABLE public."{table}" SET SCHEMA "{SCHEMA}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f'ALTER TABLE "{SCHEMA}"."{table}" SET SCHEMA public')
    op.execute(f'DROP SCHEMA "{SCHEMA}"')
