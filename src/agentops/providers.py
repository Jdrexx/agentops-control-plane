from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
    "mock": ProviderConfig("mock", "", None, "mock-small"),
    "ollama": ProviderConfig("ollama", "http://127.0.0.1:11434", None, "llama3.2"),
    "openai": ProviderConfig("openai", "https://api.openai.com", "OPENAI_API_KEY", "gpt-4o-mini"),
    "anthropic": ProviderConfig(
        "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-3-5-haiku-latest"
    ),
}


def _mock_fingerprint(model: str, system: str, prompt: str) -> str:
    """Stable key for (model, system, prompt): same inputs -> same answer, forever."""
    return hashlib.sha256("\x1f".join((model, system, prompt)).encode("utf-8")).hexdigest()


def _mock_answer(model: str, system: str, prompt: str, script_dir: Path) -> str:
    """Deterministic offline response.

    A pinned script at ``<script_dir>/<fingerprint[:16]>.txt`` wins when present,
    so demos and tests can guarantee exact output (e.g. an evaluation that fails
    on workflow v1 and passes on v2). A ``<fingerprint[:16]>.fail`` file raises a
    deterministic ``ProviderError`` with the file contents as the message —
    offline failure injection for incident demos and retry-path tests. Unpinned
    prompts fall back to stable synthetic filler derived from the fingerprint.
    """
    fingerprint = _mock_fingerprint(model, system, prompt)
    fail_script = script_dir / f"{fingerprint[:16]}.fail"
    if fail_script.is_file():
        raise ProviderError(
            fail_script.read_text(encoding="utf-8").strip() or "simulated provider outage"
        )
    pinned = script_dir / f"{fingerprint[:16]}.txt"
    if pinned.is_file():
        return pinned.read_text(encoding="utf-8").strip()
    seed = int(fingerprint[:8], 16)
    tone = ("Acknowledged", "Understood", "Confirmed", "Noted")[seed % 4]
    subject = " ".join(prompt.split()[:8]) or "the request"
    return (
        f"{tone}. Regarding {subject} -- I have reviewed the details and "
        f"recommend proceeding with the documented resolution path. "
        f"[mock:{fingerprint[:8]}]"
    )


class ProviderRegistry:
    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": config.name,
                "default_model": config.default_model,
                "configured": config.api_key_env is None or bool(os.getenv(config.api_key_env)),
                "local": config.name in {"ollama", "mock"},
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
        if provider == "mock":
            return self._generate_mock(model, prompt, system, on_chunk)
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

    def _generate_mock(
        self,
        model: str,
        prompt: str,
        system: str,
        on_chunk: Callable[[str], None] | None,
    ) -> str:
        """Deterministic offline generation with optional paced streaming.

        Environment controls (read per call so tests can override them):
        - ``AGENTOPS_MOCK_CHUNK_MS`` — delay between streamed chunks (default 35;
          set to 0 in CI so tests are instant).
        - ``AGENTOPS_MOCK_LATENCY_MS`` — fixed latency before answering.
        - ``AGENTOPS_MOCK_SCRIPT_DIR`` — directory of pinned ``<fp16>.txt``
          response scripts (default ``examples/mock_responses``).

        Streamed chunks reconstruct the returned text exactly
        (``"".join(chunks) == text``).
        """
        chunk_ms = int(os.getenv("AGENTOPS_MOCK_CHUNK_MS", "35"))
        latency_ms = int(os.getenv("AGENTOPS_MOCK_LATENCY_MS", "0"))
        script_dir = Path(os.getenv("AGENTOPS_MOCK_SCRIPT_DIR", "examples/mock_responses"))
        if latency_ms:
            time.sleep(latency_ms / 1000)
        text = _mock_answer(model, system or "", prompt, script_dir)
        if on_chunk is None:
            return text
        chunks = re.findall(r"\S+\s*", text) or [text]
        for chunk in chunks:
            if chunk_ms:
                time.sleep(chunk_ms / 1000)
            on_chunk(chunk)
        return text

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
