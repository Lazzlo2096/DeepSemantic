"""Store paper summaries as Markdown text."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0030_paper_summary_markdown"
down_revision = "0029_enum_paper_summary_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE FUNCTION pg_temp.paper_summary_markdown(document jsonb) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT AS $$
            SELECT CASE WHEN jsonb_typeof(document) = 'object' THEN concat_ws(E'\n\n',
                document->>'overview',
                E'### Задача\n\n' || (document->>'objective'),
                E'### Метод\n\n' || (document->>'method'),
                (SELECT E'### Ключевые результаты\n\n' || string_agg('- ' || item, E'\n' ORDER BY position)
                 FROM jsonb_array_elements_text(document->'results') WITH ORDINALITY AS result(item, position)),
                (SELECT E'### Ограничения\n\n' || string_agg('- ' || item, E'\n' ORDER BY position)
                 FROM jsonb_array_elements_text(document->'limitations') WITH ORDINALITY AS limitation(item, position))
            ) ELSE document #>> '{}' END;
        $$
    """)
    op.alter_column("paper_summaries", "summary", type_=sa.Text(),
                    postgresql_using="pg_temp.paper_summary_markdown(summary)")
    op.execute("DROP FUNCTION pg_temp.paper_summary_markdown(jsonb)")


def downgrade() -> None:
    # The old API can still display the entire Markdown in its overview field.
    op.alter_column("paper_summaries", "summary", type_=postgresql.JSONB(), postgresql_using="""
        CASE WHEN summary IS NOT NULL THEN jsonb_build_object(
            'overview', summary, 'objective', '—', 'method', '—',
            'results', '[]'::jsonb, 'limitations', '[]'::jsonb
        ) END
    """)
