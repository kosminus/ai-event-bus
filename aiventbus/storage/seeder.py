"""System facts seeder — auto-populates knowledge store on first run."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import socket

from aiventbus.storage.repositories import KnowledgeRepository

logger = logging.getLogger(__name__)


def _get_distro() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except FileNotFoundError:
        pass
    return platform.platform()


def _get_memory_gb() -> str:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return f"{kb / 1024 / 1024:.1f}"
    except (FileNotFoundError, ValueError):
        pass
    return "unknown"


async def _detect_gpu() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0 and stdout:
            return stdout.decode().strip()
    except (FileNotFoundError, asyncio.TimeoutError):
        pass
    return "none detected"


def _get_disk_usage() -> str:
    usage = shutil.disk_usage("/")
    total_gb = usage.total / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    return f"{total_gb:.0f}GB total, {free_gb:.0f}GB free"


async def seed_system_facts(knowledge_repo: KnowledgeRepository) -> None:
    """Populate system facts on first run. Skips if already seeded."""
    existing = await knowledge_repo.get("system.hostname")
    if existing:
        return

    gpu = await _detect_gpu()

    facts = {
        "system.hostname": socket.gethostname(),
        "system.username": os.getenv("USER", os.getenv("USERNAME", "unknown")),
        "system.distro": _get_distro(),
        "system.kernel": platform.release(),
        "system.python": platform.python_version(),
        "system.memory_gb": _get_memory_gb(),
        "system.gpu": gpu,
        "system.disk_root": _get_disk_usage(),
        "system.arch": platform.machine(),
    }

    for key, value in facts.items():
        await knowledge_repo.set(key, str(value), source="system.seeder")

    logger.info("Seeded %d system facts into knowledge store", len(facts))
