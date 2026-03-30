"""SQLite database setup, schema creation, and connection management."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
-- Events table
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL,
    priority TEXT DEFAULT 'medium',
    semantic_type TEXT,
    dedupe_key TEXT,
    dedupe_count INTEGER DEFAULT 1,
    parent_event TEXT REFERENCES events(id),
    output_topic TEXT,
    context_refs TEXT DEFAULT '[]',
    memory_scope TEXT,
    source TEXT,
    status TEXT DEFAULT 'received',
    producer_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic);
CREATE INDEX IF NOT EXISTS idx_events_semantic_type ON events(semantic_type);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_dedupe_key ON events(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_event);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);

-- Agents (consumers)
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    system_prompt TEXT DEFAULT 'You are a helpful AI agent processing events from an event bus.',
    description TEXT,
    fallback_model TEXT,
    capabilities TEXT DEFAULT '[]',
    max_concurrent INTEGER DEFAULT 1,
    queue_size INTEGER DEFAULT 50,
    memory_scope TEXT,
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'idle',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Producers
CREATE TABLE IF NOT EXISTS producers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config TEXT DEFAULT '{}',
    default_topic TEXT,
    default_semantic_type TEXT,
    default_priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'stopped',
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Routing rules
CREATE TABLE IF NOT EXISTS routing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    topic_pattern TEXT,
    semantic_type_pattern TEXT,
    min_priority TEXT,
    required_capabilities TEXT DEFAULT '[]',
    consumer_id TEXT NOT NULL REFERENCES agents(id),
    model_override TEXT,
    token_budget_override INTEGER,
    priority_order INTEGER DEFAULT 100,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rules_consumer ON routing_rules(consumer_id);
CREATE INDEX IF NOT EXISTS idx_rules_order ON routing_rules(priority_order);

-- Event-agent assignments
CREATE TABLE IF NOT EXISTS event_assignments (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    model_used TEXT,
    token_budget INTEGER,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assignments_event ON event_assignments(event_id);
CREATE INDEX IF NOT EXISTS idx_assignments_agent ON event_assignments(agent_id);
CREATE INDEX IF NOT EXISTS idx_assignments_status ON event_assignments(status);

-- Agent memory (transcript)
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    memory_scope TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    event_id TEXT REFERENCES events(id),
    token_count INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memory_agent_scope ON agent_memory(agent_id, memory_scope);

-- Agent pinned facts
CREATE TABLE IF NOT EXISTS agent_pinned_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    memory_scope TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pinned_agent_scope ON agent_pinned_facts(agent_id, memory_scope);

-- Agent responses
CREATE TABLE IF NOT EXISTS agent_responses (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES event_assignments(id),
    agent_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    response_text TEXT NOT NULL,
    parsed_output TEXT,
    output_event_id TEXT REFERENCES events(id),
    model_used TEXT,
    tokens_used INTEGER,
    duration_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_responses_assignment ON agent_responses(assignment_id);
CREATE INDEX IF NOT EXISTS idx_responses_agent ON agent_responses(agent_id);
"""


class Database:
    """Async SQLite database wrapper with WAL mode."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open connection and initialize schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        logger.info("Database initialized at %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn
