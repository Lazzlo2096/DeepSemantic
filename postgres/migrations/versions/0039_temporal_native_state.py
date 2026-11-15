"""Keep artifacts in PostgreSQL; move execution state entirely to Temporal."""
from alembic import op

revision = "0039_temporal_native_state"
down_revision = "0038_temporal_processing"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DROP VIEW current_paper_chunks;
        DROP VIEW paper_chunk_sources;
        DROP FUNCTION arxiv_import_result(uuid);

        ALTER TABLE arxiv_imports ADD COLUMN workflow_id text;
        UPDATE arxiv_imports request SET workflow_id = job.id
        FROM temporal_jobs job WHERE job.kind = 'import' AND job.entity = request.id::text;
        UPDATE arxiv_imports SET workflow_id = 'import:' || replace(id::text, '-', '') WHERE workflow_id IS NULL;
        ALTER TABLE arxiv_imports ALTER COLUMN workflow_id SET NOT NULL;

        DROP TABLE paper_index_state;
        DROP TABLE arxiv_import_items;
        DROP TABLE temporal_jobs;
        DROP TABLE arxiv_import_attempts;
        ALTER TABLE arxiv_imports
            DROP COLUMN status, DROP COLUMN attempts, DROP COLUMN available_at,
            DROP COLUMN lease_until, DROP COLUMN claim_id, DROP COLUMN started_at,
            DROP COLUMN finished_at, DROP COLUMN temporal_discovered;

        ALTER TABLE arxiv_papers DROP COLUMN status, DROP COLUMN lease_until, DROP COLUMN text_available_at;

        -- Empty request placeholders are not summary results. Completed texts stay intact.
        DELETE FROM paper_summaries WHERE summary IS NULL;
        ALTER TABLE paper_summaries
            DROP COLUMN status, DROP COLUMN attempts, DROP COLUMN available_at,
            DROP COLUMN lease_until, DROP COLUMN parts_completed, DROP COLUMN parts_total,
            DROP COLUMN last_error, DROP COLUMN created_at;
        ALTER TABLE paper_summaries RENAME COLUMN finished_at TO generated_at;
        ALTER TABLE paper_summaries ALTER COLUMN summary SET NOT NULL;
        ALTER TABLE paper_summaries ADD COLUMN generation text;
        ALTER TABLE paper_chunks ADD COLUMN generation text;
        DROP TYPE arxiv_import_status;
        DROP TYPE arxiv_paper_status;
        DROP TYPE paper_summary_status;

        CREATE VIEW paper_chunk_sources AS
        SELECT arxiv_id, 'article'::text AS source_kind,
            jsonb_build_array(bucket, text_object_key)::text AS source_version, bucket, text_object_key
        FROM arxiv_papers WHERE text_object_key IS NOT NULL
        UNION ALL
        SELECT result.arxiv_id, 'summary', md5(result.summary), NULL, NULL
        FROM paper_summaries result JOIN arxiv_papers paper USING (arxiv_id)
        WHERE result.source_version = jsonb_build_array(paper.bucket, paper.text_object_key)::text;
        CREATE VIEW current_paper_chunks AS
        SELECT link.* FROM paper_chunks link JOIN paper_chunk_sources source USING (arxiv_id, source_kind)
        WHERE link.source_version IS NULL OR link.source_version = source.source_version;
    """)


def downgrade():
    raise RuntimeError("Restore the pre-migration backup together with its matching application and Temporal history.")
