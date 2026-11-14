"""Move execution ownership from SQL leases to Temporal; retain API projections."""

from alembic import op

revision = "0038_temporal_processing"
down_revision = "0037_rag_run_metadata"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE arxiv_imports DROP CONSTRAINT arxiv_imports_running_lease_check;
        ALTER TABLE paper_summaries DROP CONSTRAINT paper_summary_lease;
        ALTER TABLE arxiv_papers DROP CONSTRAINT arxiv_papers_text_lease_check;
        UPDATE arxiv_imports SET status = CASE WHEN status = 'pausing' THEN 'paused'::arxiv_import_status
                                             ELSE 'queued'::arxiv_import_status END,
            lease_until = NULL, claim_id = NULL WHERE status IN ('running', 'pausing');
        UPDATE arxiv_papers SET status = 'downloaded', lease_until = NULL WHERE status = 'texting';
        UPDATE paper_summaries SET status = 'queued', lease_until = NULL WHERE status = 'running';
        ALTER TABLE paper_summaries ADD COLUMN source_version text;
        CREATE TABLE temporal_jobs (
            id text PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN ('import', 'text', 'summary', 'index')),
            entity text NOT NULL,
            payload jsonb NOT NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'dispatched', 'completed', 'failed', 'superseded')),
            created_at timestamptz NOT NULL DEFAULT now(),
            dispatched_at timestamptz,
            finished_at timestamptz,
            last_error text
        );
        CREATE INDEX temporal_jobs_pending_idx ON temporal_jobs (created_at, id) WHERE status = 'pending';
        CREATE TABLE paper_index_state (
            arxiv_id text NOT NULL REFERENCES arxiv_papers(arxiv_id) ON DELETE CASCADE,
            source_kind text NOT NULL,
            job_id text NOT NULL REFERENCES temporal_jobs(id),
            PRIMARY KEY (arxiv_id, source_kind)
        );
        CREATE TABLE arxiv_import_items (
            import_id uuid NOT NULL REFERENCES arxiv_imports(id) ON DELETE CASCADE,
            position integer NOT NULL,
            article jsonb NOT NULL,
            outcome text CHECK (outcome IN ('available', 'reused', 'unavailable')),
            PRIMARY KEY (import_id, position)
        );
        ALTER TABLE arxiv_imports ADD COLUMN temporal_discovered boolean NOT NULL DEFAULT false;
        CREATE OR REPLACE VIEW paper_chunk_sources AS
        SELECT arxiv_id, 'article'::text AS source_kind,
               jsonb_build_array(bucket, text_object_key)::text AS source_version, bucket, text_object_key
        FROM arxiv_papers WHERE status = 'texted' AND text_object_key IS NOT NULL
        UNION ALL
        SELECT job.arxiv_id, 'summary', md5(job.summary), NULL, NULL
        FROM paper_summaries job JOIN arxiv_papers paper USING (arxiv_id)
        WHERE job.status = 'completed' AND paper.status = 'texted'
          AND job.source_version = jsonb_build_array(paper.bucket, paper.text_object_key)::text;
    """)


def downgrade():
    # Reinstating lease ownership while Temporal executions exist would allow two owners.
    raise RuntimeError("Stop Temporal workers and restore the pre-migration backup to roll back 0038.")
