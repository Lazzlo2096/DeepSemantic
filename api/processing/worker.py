import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
import logging
import os
import signal

from temporalio.worker import Worker
from tokenizers import Tokenizer

from chunks import require_zero_chunk_overlap
from embedding_schemas import EMBEDDING_OPENROUTER_MODEL
from main import app, lifespan
from processing.activities import Activities
from processing.config import PIPELINE_QUEUE, QUEUES, temporal_client
from processing.workflows import PIPELINE_WORKFLOWS


def worker_bindings(activities, kind):
    methods = {
        "import": [activities.discover_import, activities.manifest_page, activities.download_paper],
        "text": [activities.extract], "summary": [activities.summarize],
        "index": [activities.prepare_index, activities.index_batch_activity],
        "pipeline": [activities.paper_context, activities.dependency_result, activities.catalog_page,
                     activities.pipeline_summary_mode, activities.summary_reconciliation_page],
    }
    return {"task_queue": PIPELINE_QUEUE if kind == 'pipeline' else QUEUES[kind],
            "workflows": PIPELINE_WORKFLOWS if kind == 'pipeline' else [], "activities": methods[kind]}


@asynccontextmanager
async def processing_workers(client, activities, kind="all", *, concurrency=1, shutdown_seconds=30):
    kinds = ["pipeline", *QUEUES] if kind == "all" else [kind]
    async with AsyncExitStack() as stack:
        for queue_kind in kinds:
            bindings = worker_bindings(activities, queue_kind)
            await stack.enter_async_context(Worker(
                client, **bindings,
                max_concurrent_activities=1 if queue_kind == "import" else concurrency,
                graceful_shutdown_timeout=timedelta(seconds=shutdown_seconds),
            ))
            logging.info("Temporal worker ready: %s", bindings["task_queue"])
        yield


async def run(kind="all"):
    max_tokens = int(os.getenv("EMBEDDING_MAX_TOKENS", "512"))
    tokenizer = None
    if kind in ("index", "all"):
        require_zero_chunk_overlap()
        if not 3 <= max_tokens <= 32768:
            raise ValueError("EMBEDDING_MAX_TOKENS must be between 3 and 32768")
        default_tokenizer = (
            "/app/bge-m3-tokenizer.json" if EMBEDDING_OPENROUTER_MODEL == "baai/bge-m3"
            else "/app/nemotron-3.5-embed-8b-tokenizer.json"
        )
        tokenizer = Tokenizer.from_file(os.getenv("TOKENIZER_PATH", default_tokenizer))
    client = await temporal_client()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            pass
    async with lifespan(app):
        activities = Activities(app.state, tokenizer, max_tokens, client)
        async with processing_workers(client, activities, kind,
                concurrency=int(os.getenv("TEMPORAL_ACTIVITY_CONCURRENCY", "1"))):
            await stopped.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", nargs="?", default="all", choices=[*QUEUES, "pipeline", "all"])
    asyncio.run(run(parser.parse_args().kind))
