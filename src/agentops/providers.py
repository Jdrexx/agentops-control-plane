from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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

    def generate(
        self,
        provider: str,
        model: str,
        prompt: str,
        system: str = "",
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        if provider not in PROVIDERS:
            raise ProviderError(f"unknown provider: {provider}")
        config = PROVIDERS[provider]
        model = model or config.default_model
        if provider == "ollama":
            if on_chunk:
                return self._stream_request(
                    f"{os.getenv('OLLAMA_HOST', config.endpoint).rstrip('/')}/api/generate",
                    {"model": model, "prompt": prompt, "system": system, "stream": True},
                    {},
                    lambda event: str(event.get("response", "")),
                    on_chunk,
                )
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
            payload = {"model": model, "messages": messages}
            headers = {"Authorization": f"Bearer {api_key}"}
            if on_chunk:
                return self._stream_request(
                    f"{config.endpoint}/v1/chat/completions",
                    {**payload, "stream": True},
                    headers,
                    lambda event: str(
                        event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    ),
                    on_chunk,
                    server_sent_events=True,
                )
            response = self._request(
                f"{config.endpoint}/v1/chat/completions",
                payload,
                headers,
            )
            return str(response["choices"][0]["message"]["content"])
        if on_chunk:
            return self._stream_request(
                f"{config.endpoint}/v1/messages",
                {
                    "model": model,
                    "max_tokens": 1024,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                },
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                lambda event: str(event.get("delta", {}).get("text", "")),
                on_chunk,
                server_sent_events=True,
            )
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
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # nosec B310
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"provider request failed: {error}") from error

    @staticmethod
    def _stream_request(
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        extract: Callable[[dict[str, Any]], str],
        on_chunk: Callable[[str], None],
        server_sent_events: bool = False,
    ) -> str:
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            raise ProviderError("provider endpoint must use HTTP or HTTPS")
        request = urllib.request.Request(  # noqa: S310 -- scheme restricted above.
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        chunks: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # nosec B310
                for raw_line in response:
                    line = raw_line.decode().strip()
                    if server_sent_events:
                        if not line.startswith("data:"):
                            continue
                        line = line.removeprefix("data:").strip()
                        if line == "[DONE]":
                            break
                    if not line:
                        continue
                    chunk = extract(json.loads(line))
                    if chunk:
                        chunks.append(chunk)
                        on_chunk(chunk)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"provider stream failed: {error}") from error
        return "".join(chunks)
