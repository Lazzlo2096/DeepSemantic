import argparse
import asyncio
from datetime import timedelta
import logging
import os
import signal

from temporalio.worker import Worker
from tokenizers import Tokenizer

from chunks import require_zero_chunk_overlap
from main import app, lifespan
from processing.activities import Activities
from processing.config import PIPELINE_QUEUE, QUEUES, temporal_client
from processing.workflows import PIPELINE_WORKFLOWS


def worker_bindings(activities, kind):
    methods = {
        "import": [activities.discover_import, activities.manifest_page, activities.download_paper],
        "text": [activities.extract], "summary": [activities.summarize],
        "index": [activities.prepare_index, activities.index_batch_activity],
        "pipeline": [activities.paper_context, activities.dependency_result, activities.catalog_page],
    }
    return {"task_queue": PIPELINE_QUEUE if kind == 'pipeline' else QUEUES[kind],
            "workflows": PIPELINE_WORKFLOWS if kind == 'pipeline' else [], "activities": methods[kind]}


async def run(kind):
    max_tokens = int(os.getenv("EMBEDDING_MAX_TOKENS", "512"))
    tokenizer = None
    if kind == "index":
        require_zero_chunk_overlap()
        if not 3 <= max_tokens <= 8192:
            raise ValueError("EMBEDDING_MAX_TOKENS must be between 3 and 8192")
        tokenizer = Tokenizer.from_file(os.getenv("TOKENIZER_PATH", "/app/bge-m3-tokenizer.json"))
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
        bindings = worker_bindings(activities, kind)
        async with Worker(
            client, **bindings,
            max_concurrent_activities=int(os.getenv("TEMPORAL_ACTIVITY_CONCURRENCY", "1")),
            graceful_shutdown_timeout=timedelta(seconds=30),
        ):
            logging.info("Temporal worker ready: %s", bindings["task_queue"])
            await stopped.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=[*QUEUES, "pipeline"])
    asyncio.run(run(parser.parse_args().kind))
