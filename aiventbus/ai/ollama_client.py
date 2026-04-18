"""Async Ollama HTTP client with streaming support."""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)


class ModelInfo:
    def __init__(self, name: str, size: int = 0, modified_at: str = ""):
        self.name = name
        self.size = size
        self.modified_at = modified_at


class OllamaClient:
    """Thin async wrapper around Ollama's REST API."""

    def __init__(self, base_url: str = "http://localhost:11434", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def is_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            client = await self._get_client()
            resp = await client.get("/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[ModelInfo]:
        """List available local models."""
        try:
            client = await self._get_client()
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [
                ModelInfo(
                    name=m["name"],
                    size=m.get("size", 0),
                    modified_at=m.get("modified_at", ""),
                )
                for m in data.get("models", [])
            ]
        except Exception as e:
            logger.error("Failed to list models: %s", e)
            return []

    async def check_model(self, model: str) -> bool:
        """Check if a specific model is available."""
        models = await self.list_models()
        return any(m.name == model or m.name.startswith(model + ":") for m in models)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        stats_out: dict | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion. Yields token chunks.

        If ``stats_out`` is provided, Ollama's final-chunk counters
        (``prompt_eval_count``, ``eval_count``) are written into it on
        successful completion.
        """
        client = await self._get_client()
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if options:
            payload["options"] = options

        async with client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("done"):
                        if stats_out is not None:
                            stats_out["prompt_eval_count"] = data.get("prompt_eval_count", 0)
                            stats_out["eval_count"] = data.get("eval_count", 0)
                        break
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
                except json.JSONDecodeError:
                    continue

    async def chat_sync(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
    ) -> str:
        """Non-streaming chat completion. Returns full response."""
        client = await self._get_client()
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if options:
            payload["options"] = options

        resp = await client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")
