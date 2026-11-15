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


if __name__ == '__main__':
    unittest.main(verbosity=2)
