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
from processing.config import QUEUES, temporal_client
from processing.workflows import ProcessingWorkflow


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
        activities = Activities(app.state, tokenizer, max_tokens)
        methods = {
            "import": [activities.import_control, activities.import_discover, activities.import_step],
            "text": [activities.process_text], "summary": [activities.process_summary],
            "index": [activities.prepare_index, activities.index_batch_activity],
        }
        async with Worker(
            client, task_queue=QUEUES[kind], workflows=[ProcessingWorkflow],
            activities=[activities.finish_job, *methods[kind]],
            max_concurrent_activities=int(os.getenv("TEMPORAL_ACTIVITY_CONCURRENCY", "1")),
            graceful_shutdown_timeout=timedelta(seconds=30),
        ):
            logging.info("Temporal worker ready: %s", QUEUES[kind])
            await stopped.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=QUEUES)
    asyncio.run(run(parser.parse_args().kind))
