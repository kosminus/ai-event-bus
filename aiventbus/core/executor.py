"""Executor — runs approved actions against the OS.

Each action type has a registered handler. The executor never evaluates policy —
it only executes what has been approved.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

ActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Executor:
    """Dispatches approved actions to registered handlers."""

    def __init__(self, shell_timeout: int = 30):
        self._handlers: dict[str, ActionHandler] = {}
        self._shell_timeout = shell_timeout
        self._register_builtins()

    def register(self, action_type: str, handler: ActionHandler) -> None:
        self._handlers[action_type] = handler

    async def execute(self, action_type: str, action_data: dict) -> dict[str, Any]:
        handler = self._handlers.get(action_type)
        if not handler:
            return {"error": f"No handler for action type: {action_type}"}
        try:
            return await handler(action_data)
        except Exception as e:
            logger.error("Executor error for %s: %s", action_type, e)
            return {"error": str(e)}

    def has_handler(self, action_type: str) -> bool:
        return action_type in self._handlers

    def _register_builtins(self) -> None:
        self.register("shell_exec", self._handle_shell_exec)
        self.register("file_read", self._handle_file_read)
        self.register("file_write", self._handle_file_write)
        self.register("file_delete", self._handle_file_delete)
        self.register("notify", self._handle_notify)
        self.register("open_app", self._handle_open_app)

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
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", title, message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return {"sent": True, "title": title}
        except FileNotFoundError:
            return {"error": "notify-send not found — install libnotify-bin"}
        except Exception as e:
            return {"error": str(e)}

    async def _handle_open_app(self, data: dict) -> dict:
        target = data.get("target", "")
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdg-open", target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return {"opened": True, "target": target}
        except FileNotFoundError:
            return {"error": "xdg-open not found"}
        except Exception as e:
            return {"error": str(e)}
