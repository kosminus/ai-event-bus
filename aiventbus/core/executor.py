"""Executor — runs approved actions against the OS.

Each action type has a registered handler. The executor never evaluates policy —
it only executes what has been approved.

Supports pluggable tool backends via ToolRegistry for external tools
(Playwright, MCP servers, HTTP APIs, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx

from aiventbus import platform as _platform
from aiventbus.core.tools import ToolRegistry
from aiventbus.telemetry import record_action_execution

logger = logging.getLogger(__name__)

ActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Executor:
    """Dispatches approved actions to registered handlers."""

    def __init__(self, shell_timeout: int = 30, tool_registry: ToolRegistry | None = None,
                 http_timeout: int = 30, http_max_size: int = 1_048_576):
        self._handlers: dict[str, ActionHandler] = {}
        self._shell_timeout = shell_timeout
        self._http_timeout = http_timeout
        self._http_max_size = http_max_size
        self.tool_registry = tool_registry or ToolRegistry()
        self._register_builtins()

    def register(self, action_type: str, handler: ActionHandler) -> None:
        self._handlers[action_type] = handler

    async def execute(self, action_type: str, action_data: dict) -> dict[str, Any]:
        start = time.monotonic()
        agent_id = str(action_data.get("_telemetry_agent_id") or "unknown")
        handler = self._handlers.get(action_type)
        if not handler:
            record_action_execution(agent_id, action_type, "unknown_handler", time.monotonic() - start)
            return {"error": f"No handler for action type: {action_type}"}
        try:
            result = await handler(action_data)
            outcome = "error" if isinstance(result, dict) and result.get("error") else "completed"
            record_action_execution(agent_id, action_type, outcome, time.monotonic() - start)
            return result
        except Exception as e:
            logger.error("Executor error for %s: %s", action_type, e)
            record_action_execution(agent_id, action_type, "failed", time.monotonic() - start)
            return {"error": str(e)}

    def has_handler(self, action_type: str) -> bool:
        return action_type in self._handlers

    def list_available_actions(self) -> list[dict[str, Any]]:
        """Return metadata about all available action types for prompt generation."""
        actions = []

        # Built-in action types
        builtin_docs = {
            "shell_exec": {
                "description": "Execute a shell command",
                "params": {"command": "shell command string", "cwd": "(optional) working directory", "timeout": "(optional) seconds"},
            },
            "file_read": {
                "description": "Read a file's contents",
                "params": {"path": "absolute file path"},
            },
            "file_write": {
                "description": "Write content to a file",
                "params": {"path": "absolute file path", "content": "file content string"},
            },
            "file_delete": {
                "description": "Delete a file",
                "params": {"path": "absolute file path"},
            },
            "notify": {
                "description": "Send a desktop notification",
                "params": {"title": "(optional) notification title", "message": "notification body"},
            },
            "open_app": {
                "description": "Open a URL or file with the default application",
                "params": {"target": "URL or file path"},
            },
            "http_request": {
                "description": "Make an HTTP request to fetch data from the web or APIs",
                "params": {"url": "full URL", "method": "(optional) GET/POST/PUT/DELETE, default GET", "headers": "(optional) dict of headers", "body": "(optional) request body string or dict"},
            },
            "set_knowledge": {
                "description": "Store a fact in the knowledge store",
                "params": {"key": "dot.separated.key", "value": "value string", "source": "(optional) source label"},
            },
            "get_knowledge": {
                "description": "Retrieve a fact from the knowledge store",
                "params": {"key": "exact key", "prefix": "(optional) scan by prefix"},
            },
        }

        for action_type in self._handlers:
            # Skip tool_call from the flat list — it gets its own section with backend details
            if action_type == "tool_call":
                continue
            doc = builtin_docs.get(action_type, {"description": action_type, "params": {}})
            actions.append({"action_type": action_type, **doc})

        # Tool backends (tool_call dispatch)
        tools = self.tool_registry.list_tools()
        if tools:
            tool_names = [t.name for t in tools]
            actions.append({
                "action_type": "tool_call",
                "description": f"Call a registered tool backend. Available tools: {', '.join(tool_names)}",
                "params": {"tool": "tool name", "method": "method name", "params": "dict of method parameters"},
            })

        # Always-available bus actions (handled in llm_agent, not executor)
        actions.extend([
            {
                "action_type": "emit_event",
                "description": "Publish a new event to the bus (chain reaction)",
                "params": {"topic": "event topic", "payload": "event payload dict"},
            },
            {
                "action_type": "log",
                "description": "Log a message (informational, no side effects)",
                "params": {"message": "log message"},
            },
            {
                "action_type": "alert",
                "description": "Broadcast an alert to the dashboard",
                "params": {"message": "alert message"},
            },
        ])

        return actions

    def _register_builtins(self) -> None:
        self.register("shell_exec", self._handle_shell_exec)
        self.register("file_read", self._handle_file_read)
        self.register("file_write", self._handle_file_write)
        self.register("file_delete", self._handle_file_delete)
        self.register("notify", self._handle_notify)
        self.register("open_app", self._handle_open_app)
        self.register("http_request", self._handle_http_request)
        self.register("tool_call", self._handle_tool_call)

    async def _handle_shell_exec(self, data: dict) -> dict:
        command = data.get("command", "")
        cwd = data.get("cwd")
        timeout = data.get("timeout", self._shell_timeout)

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"error": f"Command timed out after {timeout}s", "returncode": -1}

        return {
            "stdout": stdout.decode("utf-8", errors="replace")[:10000],
            "stderr": stderr.decode("utf-8", errors="replace")[:5000],
            "returncode": proc.returncode,
        }

    async def _handle_file_read(self, data: dict) -> dict:
        path = data.get("path", "")
        try:
            p = Path(path).expanduser().resolve()
            content = p.read_text(encoding="utf-8", errors="replace")
            return {"path": str(p), "content": content[:50000], "size": p.stat().st_size}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_file_write(self, data: dict) -> dict:
        path = data.get("path", "")
        content = data.get("content", "")
        try:
            p = Path(path).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return {"path": str(p), "bytes_written": len(content.encode("utf-8"))}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_file_delete(self, data: dict) -> dict:
        path = data.get("path", "")
        try:
            p = Path(path).expanduser().resolve()
            if p.is_file():
                p.unlink()
                return {"path": str(p), "deleted": True}
            return {"error": f"Not a file: {path}"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_notify(self, data: dict) -> dict:
        title = data.get("title", "AI Event Bus")
        message = data.get("message", "")
        notifier = _platform.notifier()
        if notifier is None:
            return {
                "error": (
                    "No notification backend available on this platform "
                    "(install libnotify-bin on Linux; osascript ships with macOS)"
                )
            }
        cmd = notifier.build_command(title=title, message=message)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return {"sent": True, "title": title, "backend": notifier.backend}
        except FileNotFoundError:
            return {"error": f"Notifier binary missing: {notifier.executable}"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_open_app(self, data: dict) -> dict:
        target = data.get("target", "")
        opener = _platform.opener()
        if opener is None:
            return {
                "error": (
                    "No opener backend available on this platform "
                    "(install xdg-utils on Linux; `open` ships with macOS)"
                )
            }
        cmd = opener.build_command(target=target)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return {"opened": True, "target": target, "backend": opener.backend}
        except FileNotFoundError:
            return {"error": f"Opener binary missing: {opener.executable}"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_http_request(self, data: dict) -> dict:
        """Make an HTTP request to fetch external data."""
        url = data.get("url", "")
        method = data.get("method", "GET").upper()
        headers = data.get("headers") or {}
        body = data.get("body")

        if not url:
            return {"error": "url is required"}

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout, follow_redirects=True) as client:
                kwargs: dict[str, Any] = {"headers": headers}
                if body and method in ("POST", "PUT", "PATCH"):
                    if isinstance(body, dict):
                        kwargs["json"] = body
                    else:
                        kwargs["content"] = str(body)

                resp = await client.request(method, url, **kwargs)

                # Truncate large responses
                content = resp.text[:self._http_max_size]
                return {
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": content,
                    "url": str(resp.url),
                    "truncated": len(resp.text) > self._http_max_size,
                }
        except httpx.TimeoutException:
            return {"error": f"HTTP request timed out after {self._http_timeout}s"}
        except httpx.RequestError as e:
            return {"error": f"HTTP request failed: {e}"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_tool_call(self, data: dict) -> dict:
        """Dispatch to a registered tool backend."""
        tool = data.get("tool", "")
        method = data.get("method", "")
        params = data.get("params") or {}

        if not tool:
            return {"error": "tool is required"}
        if not method:
            return {"error": "method is required"}

        return await self.tool_registry.call(tool, method, params)
