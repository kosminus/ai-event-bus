"""System facts seeder — auto-populates knowledge store on first run.

All OS detection lives in ``aiventbus.platform.platform_facts``; this
module just normalizes the values into the ``system.*`` keys that
downstream agents know about and writes them to the knowledge store.
"""

from __future__ import annotations

import logging
import shutil

from aiventbus import platform as _platform
from aiventbus.storage.repositories import KnowledgeRepository

logger = logging.getLogger(__name__)


def _disk_usage_root() -> str:
    """Capacity + free space of the root filesystem in GB, human-formatted."""
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return "unknown"
    total_gb = usage.total / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    return f"{total_gb:.0f}GB total, {free_gb:.0f}GB free"


async def seed_system_facts(knowledge_repo: KnowledgeRepository) -> None:
    """Populate system facts on first run. Skips if already seeded."""
    if await knowledge_repo.get("system.hostname"):
        return

    pf = _platform.platform_facts()

    # ``distro`` in the schema is the human-readable OS label, portable
    # across Linux (e.g. "Ubuntu 22.04") and macOS (e.g. "macOS 14.4").
    distro = f"{pf['os_name']} {pf['os_version']}".strip()
    memory = f"{pf['memory_gb']:.1f}" if isinstance(pf.get("memory_gb"), (int, float)) else "unknown"
    gpu = pf.get("gpu") or "none detected"

    facts = {
        "system.hostname":   pf["hostname"],
        "system.username":   pf["username"],
        "system.distro":     distro,
        "system.os_name":    pf["os_name"],
        "system.os_version": pf["os_version"],
        "system.kernel":     pf["kernel"],
        "system.python":     pf["python"],
        "system.memory_gb":  memory,
        "system.gpu":        gpu,
        "system.disk_root":  _disk_usage_root(),
        "system.arch":       pf["arch"],
        "system.shell":      pf["shell"],
        "system.log_tools":  pf["log_tools"],
        "system.desktop_env": pf["desktop_env"],
    }

    for key, value in facts.items():
        await knowledge_repo.set(key, str(value), source="system.seeder")

    logger.info("Seeded %d system facts into knowledge store", len(facts))
