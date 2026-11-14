"""Real Temporal/PostgreSQL/RustFS/Qdrant tests. Model and arXiv calls are local fakes.

Run using tests/temporal/compose.yaml, after migrating the legacy fixture to head.
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from temporalio import activity
from temporalio.worker import Worker, Replayer
from tokenizers import Tokenizer

from arxiv_grabber.main import Article
from chunks import PAPER_COLLECTIONS
from embedding_schemas import EMBEDDING_OPENROUTER_MODEL
from main import app, lifespan
from processing.activities import Activities
from processing.config import QUEUES, temporal_client
from processing.db import connection
from processing.dispatcher import dispatch, reconcile
from processing.workflows import ProcessingWorkflow


async def wait_until(check, timeout=45):
    async with asyncio.timeout(timeout):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(0.2)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        self.addAsyncCleanup(self.stack.aclose)
        await self.stack.enter_async_context(lifespan(app))
        self.client = await temporal_client()
        self.activities = Activities(app.state, Tokenizer.from_file('/app/bge-m3-tokenizer.json'), 512)
        try:
            app.state.rustfs.create_bucket(Bucket='papers')
        except app.state.rustfs.exceptions.BucketAlreadyOwnedByYou:
            pass

    async def asyncTearDown(self):
        async with connection() as db:
            await db.execute('TRUNCATE arxiv_imports, arxiv_papers, temporal_jobs CASCADE')

    async def workers(self, kinds):
        methods = {
            'import': [self.activities.import_control, self.activities.import_discover, self.activities.import_step],
            'text': [self.activities.process_text], 'summary': [self.activities.process_summary],
            'index': [self.activities.prepare_index, self.activities.index_batch_activity],
        }
        for kind in kinds:
            await self.stack.enter_async_context(Worker(
                self.client, task_queue=QUEUES[kind], workflows=[ProcessingWorkflow],
                activities=[self.activities.finish_job, *methods[kind]],
                graceful_shutdown_timeout=timedelta(seconds=1),
            ))

    async def cycle(self):
        async with connection() as db:
            await reconcile(db)
            await dispatch(db, self.client)

    async def jobs_done(self, kind):
        async with connection() as db:
            rows = await (await db.execute('SELECT status, last_error FROM temporal_jobs WHERE kind = %s', (kind,))).fetchall()
        if any(row['status'] == 'failed' for row in rows):
            self.fail(str(rows))
        return rows and all(row['status'] in ('completed', 'superseded') for row in rows)

    async def test_00_legacy_migration(self):
        async with connection() as db:
            row = await (await db.execute("SELECT status, lease_until FROM arxiv_imports WHERE id = '00000000-0000-0000-0000-000000000001'")).fetchone()
            self.assertEqual(row['status'], 'queued')
            self.assertIsNone(row['lease_until'])
            row = await (await db.execute("SELECT status, lease_until FROM arxiv_papers WHERE arxiv_id = '2611.99999v1'")).fetchone()
            self.assertEqual(row['status'], 'downloaded')
            self.assertIsNone(row['lease_until'])
            row = await (await db.execute("SELECT status, lease_until FROM paper_summaries WHERE arxiv_id = '2611.99999v1'")).fetchone()
            self.assertEqual(row['status'], 'queued')
            self.assertIsNone(row['lease_until'])

    async def test_full_pipeline_pause_dedup_and_source_replacement(self):
        arxiv_id = f'2611.{int(uuid4().hex[:8], 16) % 100000:05d}v1'
        articles = [Article(arxiv_id, 'Test article', ['Author'], 'Abstract', datetime(2026, 11, 1, tzinfo=timezone.utc))]
        for article in articles:
            app.state.rustfs.put_object(Bucket='papers', Key=f'arxiv/{article.arxiv_id}.html',
                                       Body=b'<article class="ltx_document"><h1>Title</h1><p>A scientific finding.</p></article>')
        async with connection() as db:
            job = await (await db.execute("""
                INSERT INTO arxiv_imports (date_from, date_to, bucket, status) VALUES ('2026-11-01', '2026-11-01', 'papers', 'paused')
                RETURNING id
            """)).fetchone()
        self.stack.enter_context(patch('processing.activities.ArxivClient.articles', return_value=iter(articles)))
        summarize = self.stack.enter_context(patch('processing.activities.Summarizer.summarize', new=AsyncMock(return_value='Scientific summary.')))

        class Embeddings:
            calls = 0
            async def embed(self, payload):
                self.calls += 1
                return {'model': EMBEDDING_OPENROUTER_MODEL, 'revision': 'test', 'dimensions': 3,
                        'embeddings': [[1.0, 0.0, 0.0] for _ in payload.texts]}

        app.state.embeddings = Embeddings()
        await self.workers(['import', 'text', 'summary', 'index'])
        await self.cycle()
        # Simulate losing the dispatch acknowledgement after Temporal accepted it.
        async with connection() as db:
            await db.execute("UPDATE temporal_jobs SET status = 'pending' WHERE kind = 'import'")
            await dispatch(db, self.client)
        await asyncio.sleep(0.5)
        async with connection() as db:
            row = await (await db.execute('SELECT status FROM arxiv_imports WHERE id = %s', (job['id'],))).fetchone()
            self.assertEqual(row['status'], 'paused')
            self.assertEqual((await (await db.execute('SELECT count(*) AS n FROM arxiv_papers')).fetchone())['n'], 0)
            await db.execute("UPDATE arxiv_imports SET status = 'queued' WHERE id = %s", (job['id'],))
        await wait_until(lambda: self.jobs_done('import'))
        await self.cycle()
        await wait_until(lambda: self.jobs_done('text'))
        await self.cycle()
        await wait_until(lambda: self.jobs_done('summary'))
        await self.cycle()
        await wait_until(lambda: self.jobs_done('index'))
        self.assertEqual(summarize.await_count, 1)
        async with connection() as db:
            stats = await (await db.execute('SELECT * FROM arxiv_import_result(%s)', (job['id'],))).fetchone()
            self.assertEqual(stats['processed_articles'], 1)
            self.assertEqual(stats['pdf_available'], 1)
            self.assertEqual((await (await db.execute("SELECT count(*) AS n FROM temporal_jobs WHERE kind = 'import'")).fetchone())['n'], 1)
            chunks = await (await db.execute('SELECT id FROM chunks JOIN paper_chunks ON chunk_id = id')).fetchall()
        self.assertGreaterEqual(len(chunks), 2)
        for collection in PAPER_COLLECTIONS.values():
            response = await app.state.qdrant.get(f'/collections/{collection}')
            response.raise_for_status()
            self.assertGreater(response.json()['result']['points_count'], 0)

        # A changed source creates a new request; the previous summary is excluded
        # from search immediately and an old Activity cannot publish over it.
        async with connection() as db:
            old = await (await db.execute("SELECT payload FROM temporal_jobs WHERE kind = 'summary'")).fetchone()
            await db.execute("UPDATE arxiv_papers SET text_object_key = 'replacement.md' WHERE arxiv_id = %s", (articles[0].arxiv_id,))
            n = await (await db.execute("SELECT count(*) AS n FROM paper_chunk_sources WHERE source_kind = 'summary'")).fetchone()
            self.assertEqual(n['n'], 0)
        app.state.rustfs.put_object(Bucket='papers', Key='replacement.md', Body=b'New source content')
        await self.cycle()
        self.assertFalse(await self.activities.process_summary.__wrapped__(self.activities, old['payload']))
        await wait_until(lambda: self.jobs_done('summary'))
        self.assertEqual(summarize.await_count, 2)
        await self.cycle()
        await wait_until(lambda: self.jobs_done('index'))

    async def test_retry_survives_worker_restart_and_replays_completed_steps(self):
        counts = {'control': 0, 'discover': 0, 'step': 0}
        failed = asyncio.Event()
        queue = f'test-restart-{uuid4().hex}'

        @activity.defn(name='import_control')
        async def control(p: dict):
            counts['control'] += 1
            return 'running'

        @activity.defn(name='import_discover')
        async def discover(p: dict):
            counts['discover'] += 1
            return True

        @activity.defn(name='import_step')
        async def step(p: dict):
            counts['step'] += 1
            if counts['step'] == 1:
                failed.set()
                raise RuntimeError('Injected worker failure')
            return False

        @activity.defn(name='finish_job')
        async def finish(p: dict):
            return None

        kwargs = dict(task_queue=queue, workflows=[ProcessingWorkflow], activities=[control, discover, step, finish])
        async with Worker(self.client, **kwargs):
            handle = await self.client.start_workflow(ProcessingWorkflow.run, {'kind': 'import'}, id=queue, task_queue=queue)
            await asyncio.wait_for(failed.wait(), 15)
        # A fresh Worker must reconstruct the workflow from persisted history.
        async with Worker(self.client, **kwargs):
            await asyncio.wait_for(handle.result(), 30)
        self.assertEqual(counts, {'control': 1, 'discover': 1, 'step': 2})
        await Replayer(workflows=[ProcessingWorkflow]).replay_workflow(await handle.fetch_history())


if __name__ == '__main__':
    unittest.main(verbosity=2)
