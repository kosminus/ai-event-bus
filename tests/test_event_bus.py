from __future__ import annotations

import json
import tempfile
import unittest

from aiventbus.config import AppConfig
from aiventbus.core.bus import EventBus, WebSocketHub
from aiventbus.models import EventCreate, EventStatus
from aiventbus.storage.db import Database
from aiventbus.storage.repositories import AssignmentRepository, EventRepository


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(json.loads(message))


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.tempdir.name}/test.db")
        await self.db.connect()

        self.config = AppConfig()
        self.event_repo = EventRepository(self.db)
        self.assignment_repo = AssignmentRepository(self.db)
        self.ws_hub = WebSocketHub()
        self.bus = EventBus(
            config=self.config,
            event_repo=self.event_repo,
            assignment_repo=self.assignment_repo,
            ws_hub=self.ws_hub,
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_publish_inherits_trace_id_from_parent(self) -> None:
        parent = await self.bus.publish(
            EventCreate(
                topic="user.query",
                payload={"text": "root"},
                trace_id="tr_parent123",
            )
        )

        child = await self.bus.publish(
            EventCreate(
                topic="agent.followup",
                payload={"text": "child"},
                parent_event=parent.id,
            )
        )

        self.assertEqual(child.trace_id, "tr_parent123")

    async def test_publish_marks_duplicates_and_updates_original_count(self) -> None:
        ws = FakeWebSocket()
        self.ws_hub.register(ws, {"events:user.query"})

        first = await self.bus.publish(
            EventCreate(
                topic="user.query",
                payload={"text": "same"},
                dedupe_key="dup-1",
            )
        )
        second = await self.bus.publish(
            EventCreate(
                topic="user.query",
                payload={"text": "same"},
                dedupe_key="dup-1",
            )
        )

        updated_first = await self.event_repo.get(first.id)

        self.assertIsNotNone(updated_first)
        self.assertEqual(second.status, EventStatus.deduped)
        self.assertEqual(updated_first.dedupe_count, 2)
        self.assertEqual(ws.messages[-1]["type"], "event.deduped")
        self.assertEqual(ws.messages[-1]["data"]["original_id"], first.id)

