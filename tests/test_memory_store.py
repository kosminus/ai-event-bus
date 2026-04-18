from __future__ import annotations

import tempfile
import unittest

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
