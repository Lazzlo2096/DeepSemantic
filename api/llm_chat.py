import os
from dataclasses import dataclass, field

import httpx


DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_OPENROUTER_PROVIDER_ORDER = "streamlake,sail-research,together"


class ChatResponseError(ValueError):
    pass


@dataclass(frozen=True)
class ChatSettings:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    reasoning_effort: str = ""
    provider_order: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, openrouter_model_env: str) -> "ChatSettings":
        provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
        if provider == "openrouter":
            return cls(provider, os.getenv(openrouter_model_env, DEFAULT_OPENROUTER_MODEL),
                       "https://openrouter.ai/api/v1/", os.getenv("OPENROUTER_API_KEY", "").strip(),
                       reasoning_effort=os.getenv("OPENROUTER_REASONING_EFFORT", "").strip(),
                       provider_order=tuple(
                           item.strip() for item in os.getenv(
                               "OPENROUTER_PROVIDER_ORDER", DEFAULT_OPENROUTER_PROVIDER_ORDER,
                           ).split(",") if item.strip()
                       ))
        if provider == "ollama":
            return cls(provider, os.getenv("OLLAMA_MODEL", "gemma3:4b"),
                       os.getenv("OLLAMA_URL", "http://ollama:11434"))
        raise ValueError("LLM_PROVIDER must be openrouter or ollama")

    @property
    def configured(self) -> bool:
        return self.provider == "ollama" or bool(self.api_key)

    async def complete(self, client: httpx.AsyncClient, messages: list[dict[str, str]], *,
                       max_tokens: int | None, ollama_options: dict, schema: dict | None = None) -> str:
        message = await self.turn(
            client, messages, max_tokens=max_tokens, ollama_options=ollama_options, schema=schema,
        )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ChatResponseError("Empty response")
        return content.strip()

    async def turn(self, client: httpx.AsyncClient, messages: list[dict], *,
                   max_tokens: int | None, ollama_options: dict, schema: dict | None = None,
                   tools: list[dict] | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        if self.provider == "openrouter":
            if not self.api_key:
                raise RuntimeError("OPENROUTER_API_KEY is required")
            reasoning = {"effort": self.reasoning_effort} if self.reasoning_effort else {"enabled": False}
            payload.update(temperature=0.1, reasoning=reasoning)
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if schema is not None:
                payload["response_format"] = {
                    "type": "json_schema", "json_schema": {"name": "rag_answer", "strict": True, "schema": schema},
                }
            routing = {}
            if self.provider_order:
                routing["order"] = list(self.provider_order)
            if schema is not None or tools:
                routing["require_parameters"] = True
            if routing:
                payload["provider"] = routing
            response = await client.post("chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
        else:
            payload.update(truncate=False, shift=False,
                           options={"num_predict": max_tokens, "temperature": 0.1, **ollama_options})
            if schema is not None:
                payload["format"] = schema
            response = await client.post("/api/chat", json=payload)
        response.raise_for_status()

        try:
            data = response.json()
            if self.provider == "openrouter":
                choice = data["choices"][0]
                allowed_reasons = ("stop", "tool_calls") if tools else ("stop",)
                if choice["finish_reason"] not in allowed_reasons:
                    raise ChatResponseError(f"finish_reason={choice['finish_reason']!r}")
                message = choice["message"]
            else:
                if data["done"] is not True or data.get("done_reason") == "length":
                    raise ChatResponseError("Incomplete generation")
                message = data["message"]
        except (IndexError, KeyError, TypeError) as error:
            raise ChatResponseError("Invalid response") from error
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            raise ChatResponseError("Invalid assistant message")
        fields = ("content", "tool_calls", "reasoning", "reasoning_details", "thinking")
        return {"role": "assistant", **{field: message[field] for field in fields if field in message}}
