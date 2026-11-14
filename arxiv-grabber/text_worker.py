"""Compatibility entry point; execution is owned by Temporal."""
import asyncio
from processing.worker import run

if __name__ == "__main__":
    asyncio.run(run("text"))
