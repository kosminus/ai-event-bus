"""Tool backends — pluggable external tool system for LLM agents.

Tool backends let agents call external systems (Playwright, MCP servers,
HTTP APIs, custom scripts) through a unified dispatch mechanism.

Each backend registers itself with the ToolRegistry, declaring its
available methods and their parameter schemas. The executor dispatches
``tool_call`` actions to the appropriate backend.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolMethod:
    """A single callable method on a tool backend."""
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON Schema


@dataclass
class ToolInfo:
    """Metadata about a registered tool backend (for prompt generation)."""
    name: str
    description: str
    methods: list[ToolMethod]


class ToolBackend(ABC):
    """Abstract base for pluggable tool backends.

    Subclass this to add new capabilities (Playwright, MCP, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name (e.g. 'playwright', 'mcp_weather')."""

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description for LLM prompt."""

    @abstractmethod
    def methods(self) -> list[ToolMethod]:
        """Declare available methods and their parameters."""

    @abstractmethod
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a method with the given parameters.

        Returns a result dict. Errors should be returned as
        ``{"error": "..."}`` rather than raised.
        """

    def info(self) -> ToolInfo:
        return ToolInfo(name=self.name, description=self.description, methods=self.methods())


class ToolRegistry:
    """Registry of tool backends. Handles dispatch and introspection."""

    def __init__(self) -> None:
        self._backends: dict[str, ToolBackend] = {}

    def register(self, backend: ToolBackend) -> None:
        if backend.name in self._backends:
            logger.warning("Replacing existing tool backend: %s", backend.name)
        self._backends[backend.name] = backend
        logger.info("Registered tool backend: %s", backend.name)

    def unregister(self, name: str) -> None:
        self._backends.pop(name, None)

    def get(self, name: str) -> ToolBackend | None:
        return self._backends.get(name)

    def list_tools(self) -> list[ToolInfo]:
        return [b.info() for b in self._backends.values()]

    async def call(self, tool: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        backend = self._backends.get(tool)
        if not backend:
            return {"error": f"Unknown tool backend: {tool}"}

        valid_methods = {m.name for m in backend.methods()}
        if method not in valid_methods:
            return {"error": f"Tool '{tool}' has no method '{method}'. Available: {sorted(valid_methods)}"}

        try:
            return await backend.call(method, params)
        except Exception as e:
            logger.error("Tool %s.%s failed: %s", tool, method, e)
            return {"error": str(e)}
