import os
from dataclasses import dataclass, field

import httpx


DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash-1017"


class ChatResponseError(ValueError):
    pass


@dataclass(frozen=True)
class ChatSettings:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    reasoning_effort: str = ""

    @classmethod
    def from_env(cls, *, openrouter_model_env: str) -> "ChatSettings":
        provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
        if provider == "openrouter":
            return cls(provider, os.getenv(openrouter_model_env, DEFAULT_OPENROUTER_MODEL),
                       "https://openrouter.ai/api/v1/", os.getenv("OPENROUTER_API_KEY", "").strip(),
                       reasoning_effort=os.getenv("OPENROUTER_REASONING_EFFORT", "").strip())
        if provider == "ollama":
            return cls(provider, os.getenv("OLLAMA_MODEL", "gemma3:4b"),
                       os.getenv("OLLAMA_URL", "http://ollama:11434"))
        raise ValueError("LLM_PROVIDER must be openrouter or ollama")

    @property
    def configured(self) -> bool:
        return self.provider == "ollama" or bool(self.api_key)

    async def complete(self, client: httpx.AsyncClient, messages: list[dict[str, str]], *,
                       max_tokens: int, ollama_options: dict, schema: dict | None = None) -> str:
        payload = {"model": self.model, "messages": messages, "stream": False}
        if self.provider == "openrouter":
            if not self.api_key:
                raise RuntimeError("OPENROUTER_API_KEY is required")
            reasoning = {"effort": self.reasoning_effort} if self.reasoning_effort else {"enabled": False}
            payload.update(max_tokens=max_tokens, temperature=0.1, reasoning=reasoning)
            if schema is not None:
                payload["response_format"] = {
                    "type": "json_schema", "json_schema": {"name": "rag_answer", "strict": True, "schema": schema},
                }
                payload["provider"] = {"require_parameters": True}
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
                if choice["finish_reason"] != "stop":
                    raise ChatResponseError(f"finish_reason={choice['finish_reason']!r}")
                content = choice["message"]["content"]
            else:
                if data["done"] is not True or data.get("done_reason") == "length":
                    raise ChatResponseError("Incomplete generation")
                content = data["message"]["content"]
        except (IndexError, KeyError, TypeError) as error:
            raise ChatResponseError("Invalid response") from error
        if not isinstance(content, str) or not content.strip():
            raise ChatResponseError("Empty response")
        return content.strip()
