from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from aiventbus.models import MemoryKind, MemoryRecordCreate
from aiventbus.storage.db import Database
from aiventbus.storage.repositories import MemoryStore


class MemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.tempdir.name}/test.db")
        await self.db.connect()
        self.store = MemoryStore(self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_search_prefers_agent_scope_over_global(self) -> None:
        await self.store.add(
            MemoryRecordCreate(
                kind=MemoryKind.semantic,
                scope="global",
                content="Builds in this repo often need pytest first.",
                summary="Global build advice",
                importance=0.8,
            )
        )
        agent_memory = await self.store.add(
            MemoryRecordCreate(
                kind=MemoryKind.semantic,
                scope="agent:agent_terminal-helper",
                content="Builds in this repo often need pytest first.",
                summary="Agent-specific build advice",
                importance=0.8,
            )
        )

        results = await self.store.search(
            query='"builds" OR "pytest"',
            scopes=["agent:agent_terminal-helper", "user", "global"],
            limit=2,
        )

        self.assertEqual(results[0].id, agent_memory.id)

    async def test_touch_updates_access_metadata(self) -> None:
        memory = await self.store.add(
            MemoryRecordCreate(
                kind=MemoryKind.episodic,
                scope="user",
                content="User denied destructive filesystem action.",
                summary="Denied destructive action",
                importance=0.9,
            )
        )

        await self.store.touch([memory.id])
        updated = await self.store.get(memory.id)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.access_count, 1)
        self.assertIsNotNone(updated.last_accessed_at)

    async def test_search_sanitizes_public_query(self) -> None:
        await self.store.add(
            MemoryRecordCreate(
                kind=MemoryKind.semantic,
                scope="global",
                content="Network calls to api.openai.com fail on this machine.",
                summary="External API calls fail locally",
                importance=0.7,
            )
        )

        results = await self.store.list(q='api.openai.com:443 OR "network"', limit=5)

        self.assertEqual(len(results), 1)

    async def test_touch_does_not_reset_recency_decay(self) -> None:
        older = await self.store.add(
            MemoryRecordCreate(
                kind=MemoryKind.episodic,
                scope="user",
                content="Old incident memory about destructive action.",
                summary="Old incident memory",
                importance=0.9,
            )
        )
        newer = await self.store.add(
            MemoryRecordCreate(
                kind=MemoryKind.episodic,
                scope="user",
                content="Recent incident memory about destructive action.",
                summary="Recent incident memory",
                importance=0.9,
            )
        )

        old_created = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        await self.db.conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (old_created, older.id),
        )
        await self.db.conn.commit()
        await self.store.touch([older.id])

        results = await self.store.search(
            query="destructive action",
            scopes=["user"],
            limit=2,
        )

        self.assertEqual(results[0].id, newer.id)
