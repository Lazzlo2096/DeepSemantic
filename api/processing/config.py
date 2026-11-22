import hashlib
import json
import os

QUEUES = {kind: f"deepsemantic-{kind}" for kind in ("import", "text", "summary", "index")}
PIPELINE_QUEUE = "deepsemantic-pipeline"
SUMMARY_PROMPT_VERSION = "5"
CHUNKER_VERSION = "markdown-offsets-v1"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def pipeline_settings():
    return {
        "summary": {
            "model": os.getenv("SUMMARY_OPENROUTER_MODEL", os.getenv("RAG_OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017")),
            "prompt_version": SUMMARY_PROMPT_VERSION,
            "reasoning_effort": os.getenv("SUMMARY_REASONING_EFFORT", ""),
            "provider_order": [value.strip() for value in os.getenv("OPENROUTER_PROVIDER_ORDER", "streamlake,sail-research,together").split(",") if value.strip()],
        },
        "index": {"model": os.getenv(
            "EMBEDDING_OPENROUTER_MODEL", "nvidia/nemotron-3.5-embed-8b:free",
        ),
                  "max_tokens": int(os.getenv("EMBEDDING_MAX_TOKENS", "512")), "chunker": CHUNKER_VERSION},
    }


def summary_generation(paper, settings):
    return fingerprint([paper["arxiv_id"], paper["markdown_version"], settings["summary"]])


def summary_workflow_id(paper, settings):
    return "summary:" + summary_generation(paper, settings)


def paper_workflow_id(paper, settings):
    return "paper:" + fingerprint([paper["arxiv_id"], paper["source_version"], settings])


async def temporal_client():
    from temporalio.client import Client
    return await Client.connect(os.getenv("TEMPORAL_ADDRESS", "temporal:7233"),
                                namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
