"""Compatibility entry point; execution is owned by Temporal."""
import asyncio
from processing.summarizer import Summarizer, SummaryError, SummaryInputError

if __name__ == "__main__":
    from processing.worker import run
    asyncio.run(run("summary"))
