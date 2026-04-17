"""System API — health, status, topics."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["system"])

_db = None
_config = None


def init(db, config):
    global _db, _config
    _db = db
    _config = config


@router.get("/system/status")
async def system_status():
    """Bus health and basic stats."""
    cursor = await _db.conn.execute("SELECT COUNT(*) as cnt FROM events")
    event_count = (await cursor.fetchone())["cnt"]

    cursor = await _db.conn.execute("SELECT COUNT(*) as cnt FROM agents")
    agent_count = (await cursor.fetchone())["cnt"]

    cursor = await _db.conn.execute("SELECT COUNT(*) as cnt FROM producers")
    producer_count = (await cursor.fetchone())["cnt"]

    cursor = await _db.conn.execute(
        "SELECT COUNT(*) as cnt FROM event_assignments WHERE status IN ('pending', 'claimed', 'running')"
    )
    active_assignments = (await cursor.fetchone())["cnt"]

    return {
        "status": "ok",
        "events_total": event_count,
        "agents_total": agent_count,
        "producers_total": producer_count,
        "active_assignments": active_assignments,
        "config": {
            "ollama_url": _config.ollama.base_url,
            "dedupe_window": _config.bus.dedupe_window_seconds,
            "max_chain_depth": _config.bus.max_chain_depth,
            "max_fan_out": _config.bus.max_fan_out,
        },
        "config_source": {
            "config_path": _config.sources.config_path,
            "config_path_source": _config.sources.config_path_source,
            "db_path": _config.sources.db_path,
            "db_path_source": _config.sources.db_path_source,
            "dev_mode": _config.sources.dev_mode,
        },
    }


@router.get("/topics")
async def list_topics():
    """List all topics with event counts and last activity."""
    cursor = await _db.conn.execute(
        """SELECT topic, COUNT(*) as event_count,
           MAX(created_at) as last_activity
           FROM events GROUP BY topic ORDER BY last_activity DESC"""
    )
    rows = await cursor.fetchall()
    return [
        {
            "topic": r["topic"],
            "event_count": r["event_count"],
            "last_activity": r["last_activity"],
        }
        for r in rows
    ]
