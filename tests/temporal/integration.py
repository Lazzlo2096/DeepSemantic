"""Optional integration scenarios for native Temporal state; run explicitly."""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from temporalio.worker import Worker
from tokenizers import Tokenizer

from main import app, lifespan
import paper_repository as repository
from processing.activities import Activities
from processing.arxiv import Article
from processing.config import temporal_client
from processing.worker import worker_bindings
from embedding_schemas import EMBEDDING_OPENROUTER_MODEL
from chunks import PAPER_COLLECTIONS
from processing.config import paper_workflow_id, pipeline_settings


class NativeStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        await self.stack.enter_async_context(lifespan(app))
        self.client = await temporal_client()
        app.state.temporal_client = self.client
        self.addAsyncCleanup(self.stack.aclose)

    async def test_00_native_schema(self):
        async with repository.connection() as db:
            columns = await (await db.execute("""SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name IN ('arxiv_imports', 'arxiv_papers', 'paper_summaries')""")).fetchall()
            self.assertFalse({'status', 'lease_until', 'attempts', 'available_at', 'claim_id'} & {row['column_name'] for row in columns})
            removed = await (await db.execute("""SELECT tablename FROM pg_tables WHERE schemaname = 'public'
                AND tablename IN ('temporal_jobs', 'paper_index_state', 'arxiv_import_items', 'arxiv_import_attempts')""")).fetchall()
            self.assertEqual(removed, [])
            self.assertIsNotNone(await repository.get_paper('2611.99999v1'))

    async def test_pipeline_uses_temporal_progress_and_signals(self):
        arxiv_id = f'2611.{int(uuid4().hex[:8], 16) % 100000:05d}v1'
        article = Article(arxiv_id, 'Native pipeline', ['Author'], 'Abstract', datetime(2026, 11, 1, tzinfo=timezone.utc))
        try:
            app.state.rustfs.create_bucket(Bucket='papers')
        except app.state.rustfs.exceptions.BucketAlreadyOwnedByYou:
            pass
        app.state.rustfs.put_object(Bucket='papers', Key=f'arxiv/{arxiv_id}.html',
            Body=b'<article class="ltx_document"><h1>Finding</h1><p>A measured result.</p></article>')
        self.stack.enter_context(patch('processing.activities.ArxivClient.articles', return_value=iter([article])))
        self.stack.enter_context(patch('processing.activities.Summarizer.summarize', new=AsyncMock(return_value='Summary result.')))
        class Embeddings:
            async def embed(self, payload):
                return {'model': EMBEDDING_OPENROUTER_MODEL, 'revision': 'test', 'dimensions': 3,
                        'embeddings': [[1., 0., 0.] for _ in payload.texts]}
        app.state.embeddings = Embeddings()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
            response = await api.post('/arxiv/imports', json={'request_id': str(uuid4()),
                'date_from': '2026-11-01', 'date_to': '2026-11-01', 'bucket': 'papers'})
            self.assertEqual(response.status_code, 202)
            identity = 'import:' + response.json()['id']
            await self.client.get_workflow_handle(identity).signal('set_paused', True)
            activities = Activities(app.state, Tokenizer.from_file('/app/bge-m3-tokenizer.json'), 512, self.client)
            for kind in ['pipeline', 'import', 'text', 'summary', 'index']:
                await self.stack.enter_async_context(Worker(self.client, **worker_bindings(activities, kind),
                    graceful_shutdown_timeout=timedelta(seconds=1)))
            await asyncio.sleep(.5)
            handle = self.client.get_workflow_handle(identity)
            self.assertTrue((await handle.query('progress'))['paused'])
            await handle.signal('set_paused', False)
            result = await asyncio.wait_for(handle.result(), 60)
            self.assertEqual(result['processed_articles'], 1)
            async with asyncio.timeout(60):
                while await repository.get_summary(arxiv_id) is None:
                    await asyncio.sleep(.2)
            summary = await repository.get_summary(arxiv_id)
            self.assertEqual(summary['summary'], 'Summary result.')
            rows = (await api.get('/arxiv/imports')).json()
            row = next(row for row in rows if row['id'] == response.json()['id'])
            self.assertEqual(row['status'], 'completed')

    async def test_rest_deferral_catchup_and_index_repair(self):
        """All workers stay online: REST alone controls paid summary processing."""
        try:
            app.state.rustfs.create_bucket(Bucket='papers')
        except app.state.rustfs.exceptions.BucketAlreadyOwnedByYou:
            pass
        # The migrated HTML-only article must be caught up too.
        app.state.rustfs.put_object(Bucket='papers', Key='legacy.html',
            Body=b'<article class="ltx_document"><h1>Legacy</h1><p>Previously downloaded article.</p></article>')
        summarizer = self.stack.enter_context(patch('processing.activities.Summarizer.summarize',
            new=AsyncMock(return_value='Summary result for deferred processing.')))
        class Embeddings:
            async def embed(self, payload):
                return {'model': EMBEDDING_OPENROUTER_MODEL, 'revision': 'test', 'dimensions': 3,
                        'embeddings': [[1., 0., 0.] for _ in payload.texts]}
        app.state.embeddings = Embeddings()
        activities = Activities(app.state, Tokenizer.from_file('/app/bge-m3-tokenizer.json'), 512, self.client)
        for kind in ['pipeline', 'import', 'text', 'summary', 'index']:
            await self.stack.enter_async_context(Worker(self.client, **worker_bindings(activities, kind),
                graceful_shutdown_timeout=timedelta(seconds=1)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
            response = await api.put('/pipeline/settings', json={'summary_mode': 'deferred'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((await api.get('/pipeline/settings')).json()['summary_mode'], 'deferred')
            ids = []
            for _ in range(2):
                arxiv_id = f'2611.{int(uuid4().hex[:8], 16) % 100000:05d}v1'
                ids.append(arxiv_id)
                article = Article(arxiv_id, 'Deferred article', ['Author'], 'Abstract', datetime.now(timezone.utc))
                key = f'arxiv/{arxiv_id}.html'
                app.state.rustfs.put_object(Bucket='papers', Key=key,
                    Body=b'<article class="ltx_document"><h1>Finding</h1><p>Article indexed while summaries are deferred.</p></article>')
                # Exercise the public import endpoint for the normal path.
                with patch('processing.activities.ArxivClient.articles', return_value=iter([article])):
                    response = await api.post('/arxiv/imports', json={'request_id': str(uuid4()),
                        'date_from': '2026-11-01', 'date_to': '2026-11-01', 'bucket': 'papers'})
                    self.assertEqual(response.status_code, 202, response.text)
                    await asyncio.wait_for(self.client.get_workflow_handle('import:' + response.json()['id']).result(), 60)
                paper = await repository.get_paper(arxiv_id)
                result = await asyncio.wait_for(self.client.get_workflow_handle(paper_workflow_id(paper, pipeline_settings())).result(), 60)
                self.assertEqual(result['article_index'], 'completed')
                self.assertEqual(result['summary_index'], 'deferred')
                progress = (await api.get(f'/papers/{arxiv_id}/processing')).json()
                self.assertEqual(progress['article_index']['status'], 'completed')
                self.assertEqual(progress['summary_index']['status'], 'deferred')
                response = await api.post(f'/papers/{arxiv_id}/summary')
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()['status'], 'deferred')
                self.assertIsNone(await repository.get_summary(arxiv_id))
            summarizer.assert_not_awaited()
            response = await api.put('/pipeline/settings', json={'summary_mode': 'enabled'})
            self.assertEqual(response.status_code, 200, response.text)

            async def wait_indexed(arxiv_id):
                async with asyncio.timeout(90):
                    while True:
                        response = await api.get(f'/papers/{arxiv_id}/processing')
                        self.assertEqual(response.status_code, 200, response.text)
                        if response.json()['summary_index']['status'] == 'completed':
                            return response.json()
                        await asyncio.sleep(.2)
            for arxiv_id in [*ids, '2611.99999v1']:
                progress = await wait_indexed(arxiv_id)
                self.assertGreater(progress['summary_index']['chunks_indexed'], 0)
                async with repository.connection() as db:
                    chunks = await (await db.execute("""SELECT chunk_id FROM current_paper_chunks
                        WHERE arxiv_id = %s AND source_kind = 'summary'""", (arxiv_id,))).fetchall()
                response = await app.state.qdrant.post(f'/collections/{PAPER_COLLECTIONS["summary"]}/points',
                    json={'ids': [row['chunk_id'] for row in chunks]})
                response.raise_for_status()
                self.assertEqual(len(response.json()['result']), len(chunks))

            # A closed SummaryWorkflow must not prevent recovery of missing vectors.
            arxiv_id = ids[0]
            calls_before = summarizer.await_count
            async with repository.connection() as db:
                chunk = await (await db.execute("""SELECT chunk_id FROM current_paper_chunks
                    WHERE arxiv_id = %s AND source_kind = 'summary' LIMIT 1""", (arxiv_id,))).fetchone()
                await db.execute('UPDATE chunks SET qdrant_point_id = NULL WHERE id = %s', (chunk['chunk_id'],))
            response = await app.state.qdrant.post(f'/collections/{PAPER_COLLECTIONS["summary"]}/points/delete?wait=true',
                json={'points': [chunk['chunk_id']]})
            response.raise_for_status()
            await api.put('/pipeline/settings', json={'summary_mode': 'enabled'})
            await wait_indexed(arxiv_id)
            self.assertEqual(summarizer.await_count, calls_before)
            response = await app.state.qdrant.post(f'/collections/{PAPER_COLLECTIONS["summary"]}/points',
                json={'ids': [chunk['chunk_id']]})
            self.assertEqual(len(response.json()['result']), 1)
            # Repeating enable and scanning an already complete catalog is free.
            await api.put('/pipeline/settings', json={'summary_mode': 'enabled'})
            await asyncio.sleep(1)
            self.assertEqual(summarizer.await_count, calls_before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
