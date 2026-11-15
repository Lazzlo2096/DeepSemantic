"""Provider selection for the OpenRouter-compatible embeddings API."""
import math
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str
    base_url: str
    timeout: float | None
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "EmbeddingSettings":
        provider = os.getenv("EMBEDDING_PROVIDER", "deepkaravan").strip().lower()
        urls = {
            "deepkaravan": "http://deepkaravan:8000/api/v1/",
            "openrouter": "https://openrouter.ai/api/v1/",
        }
        if provider not in urls:
            raise ValueError("EMBEDDING_PROVIDER must be deepkaravan or openrouter")
        timeout = float(os.getenv("EMBEDDING_TIMEOUT", "60"))
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("EMBEDDING_TIMEOUT must be finite and non-negative (0 disables the HTTP timeout)")
        if timeout == 0:
            timeout = None
        base_url = os.getenv("EMBEDDING_BASE_URL", "").strip() or urls[provider]
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip() if provider == "openrouter" else ""
        return cls(provider, base_url.rstrip("/") + "/", timeout, api_key)
