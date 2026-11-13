"""LangChain model configuration for the RAG graph."""

import httpx
from langchain_openrouter import ChatOpenRouter
from openrouter import OpenRouter

from llm_chat import ChatSettings


def create_rag_model(settings: ChatSettings, client: httpx.AsyncClient) -> ChatOpenRouter:
    # Reuse the application's client and its unlimited inference timeout.
    sdk = OpenRouter(
        api_key=settings.api_key,
        server_url=settings.base_url.rstrip("/"),
        async_client=client,
        retry_config=None,
    )
    provider = {"require_parameters": True}
    if settings.provider_order:
        provider["order"] = list(settings.provider_order)
    return ChatOpenRouter(
        client=sdk,
        api_key=settings.api_key,
        model=settings.model,
        temperature=0.1,
        reasoning={"effort": settings.reasoning_effort or "none"},
        openrouter_provider=provider,
        max_retries=0,
        cache=False,
    )
