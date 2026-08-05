from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    endpoint: str
    api_key_env: str | None
    default_model: str


PROVIDERS = {
    "ollama": ProviderConfig("ollama", "http://127.0.0.1:11434", None, "llama3.2"),
    "openai": ProviderConfig("openai", "https://api.openai.com", "OPENAI_API_KEY", "gpt-4o-mini"),
    "anthropic": ProviderConfig(
        "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-3-5-haiku-latest"
    ),
}


class ProviderRegistry:
    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": config.name,
                "default_model": config.default_model,
                "configured": config.api_key_env is None or bool(os.getenv(config.api_key_env)),
                "local": config.name == "ollama",
            }
            for config in PROVIDERS.values()
        ]

    def generate(self, provider: str, model: str, prompt: str, system: str = "") -> str:
        if provider not in PROVIDERS:
            raise ProviderError(f"unknown provider: {provider}")
        config = PROVIDERS[provider]
        model = model or config.default_model
        if provider == "ollama":
            response = self._request(
                f"{os.getenv('OLLAMA_HOST', config.endpoint).rstrip('/')}/api/generate",
                {"model": model, "prompt": prompt, "system": system, "stream": False},
                {},
            )
            return str(response.get("response", ""))
        api_key = os.getenv(config.api_key_env or "")
        if not api_key:
            raise ProviderError(f"{config.api_key_env} is not configured")
        if provider == "openai":
            messages = ([{"role": "system", "content": system}] if system else []) + [
                {"role": "user", "content": prompt}
            ]
            response = self._request(
                f"{config.endpoint}/v1/chat/completions",
                {"model": model, "messages": messages},
                {"Authorization": f"Bearer {api_key}"},
            )
            return str(response["choices"][0]["message"]["content"])
        response = self._request(
            f"{config.endpoint}/v1/messages",
            {
                "model": model,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return str(response["content"][0]["text"])

    @staticmethod
    def _request(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            raise ProviderError("provider endpoint must use HTTP or HTTPS")
        request = urllib.request.Request(  # noqa: S310 -- scheme restricted above.
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"provider request failed: {error}") from error
