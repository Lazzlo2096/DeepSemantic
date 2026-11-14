import hashlib
import json
import os


QUEUES = {kind: f"deepsemantic-{kind}" for kind in ("import", "text", "summary", "index")}
SUMMARY_PROMPT_VERSION = "5"
CHUNKER_VERSION = "markdown-offsets-v1"


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def summary_model() -> str:
    return os.getenv("SUMMARY_OPENROUTER_MODEL", os.getenv("RAG_OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017")).strip()


async def temporal_client():
    from temporalio.client import Client

    return await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
