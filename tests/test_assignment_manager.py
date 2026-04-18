from __future__ import annotations

import tempfile
import unittest

from aiventbus.config import AppConfig
from aiventbus.core.assignments import AssignmentManager
from aiventbus.models import AgentCreate, Event, RoutingRuleCreate
from aiventbus.storage.db import Database
from aiventbus.storage.repositories import (
    AgentRepository,
    AssignmentRepository,
    EventRepository,
    RoutingRuleRepository,
)


class AssignmentManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(f"{self.tempdir.name}/test.db")
        await self.db.connect()

        self.config = AppConfig()
        self.event_repo = EventRepository(self.db)
        self.agent_repo = AgentRepository(self.db)
        self.assignment_repo = AssignmentRepository(self.db)
        self.rule_repo = RoutingRuleRepository(self.db)
        self.manager = AssignmentManager(
            config=self.config,
            event_repo=self.event_repo,
            agent_repo=self.agent_repo,
            assignment_repo=self.assignment_repo,
            rule_repo=self.rule_repo,
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tempdir.cleanup()

    async def test_route_event_dedupes_same_agent_and_uses_first_rule_override(self) -> None:
        agent = await self.agent_repo.create(AgentCreate(name="Primary Agent", model="model-a"))
        disabled_agent = await self.agent_repo.create(AgentCreate(name="Disabled Agent", model="model-b"))
        await self.agent_repo.update_status(disabled_agent.id, "disabled")

        first_rule = await self.rule_repo.create(
            RoutingRuleCreate(
                name="first",
                topic_pattern="user.*",
                consumer_id=agent.id,
                model_override="fast-model",
                token_budget_override=256,
                priority_order=10,
            )
        )
        await self.rule_repo.create(
            RoutingRuleCreate(
                name="duplicate-agent",
                topic_pattern="user.*",
                consumer_id=agent.id,
                model_override="slow-model",
                token_budget_override=1024,
                priority_order=20,
            )
        )
        await self.rule_repo.create(
            RoutingRuleCreate(
                name="disabled-agent",
                topic_pattern="user.*",
                consumer_id=disabled_agent.id,
                priority_order=5,
            )
        )

        event = Event(topic="user.query", payload={"text": "hello"})
        await self.event_repo.create(event)

        matches = await self.manager.route_event(event)
        assignments = await self.assignment_repo.get_for_event(event.id)
        stored_event = await self.event_repo.get(event.id)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].agent_id, agent.id)
        self.assertEqual(matches[0].rule_id, first_rule.id)
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].agent_id, agent.id)
        self.assertEqual(assignments[0].lane.value, "interactive")
        self.assertEqual(assignments[0].model_used, "fast-model")
        self.assertEqual(assignments[0].token_budget, 256)
        self.assertEqual(stored_event.status.value, "assigned")

