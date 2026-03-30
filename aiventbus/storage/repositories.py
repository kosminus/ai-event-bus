"""CRUD operations for all entities."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from aiventbus.models import (
    Agent,
    AgentCreate,
    AgentResponse,
    Event,
    EventAssignment,
    EventCreate,
    EventStatus,
    AssignmentStatus,
    MemoryEntry,
    PinnedFact,
    Producer,
    ProducerCreate,
    RoutingRule,
    RoutingRuleCreate,
)
from aiventbus.storage.db import Database

logger = logging.getLogger(__name__)


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-").replace("_", "-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Events ---

class EventRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, event: Event) -> Event:
        await self.db.conn.execute(
            """INSERT INTO events (id, timestamp, topic, payload, priority, semantic_type,
               dedupe_key, dedupe_count, parent_event, output_topic, context_refs,
               memory_scope, source, status, producer_id, expires_at, max_retries, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.id, event.timestamp.isoformat(), event.topic,
                json.dumps(event.payload), event.priority.value,
                event.semantic_type, event.dedupe_key, event.dedupe_count,
                event.parent_event, event.output_topic,
                json.dumps(event.context_refs), event.memory_scope,
                event.source, event.status.value, event.producer_id,
                event.expires_at.isoformat() if event.expires_at else None,
                event.max_retries,
                event.created_at.isoformat(),
            ),
        )
        await self.db.conn.commit()
        return event

    async def get(self, event_id: str) -> Event | None:
        cursor = await self.db.conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_event(row)

    async def list(
        self,
        topic: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Event]:
        query = "SELECT * FROM events WHERE 1=1"
        params: list = []
        if topic:
            query += " AND topic = ?"
            params.append(topic)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor = await self.db.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_event(r) for r in rows]

    async def update_status(self, event_id: str, status: EventStatus) -> None:
        await self.db.conn.execute(
            "UPDATE events SET status = ? WHERE id = ?",
            (status.value, event_id),
        )
        await self.db.conn.commit()

    async def increment_dedupe(self, event_id: str) -> int:
        await self.db.conn.execute(
            "UPDATE events SET dedupe_count = dedupe_count + 1 WHERE id = ?",
            (event_id,),
        )
        await self.db.conn.commit()
        cursor = await self.db.conn.execute(
            "SELECT dedupe_count FROM events WHERE id = ?", (event_id,)
        )
        row = await cursor.fetchone()
        return row["dedupe_count"] if row else 0

    async def find_by_dedupe_key(self, dedupe_key: str, window_seconds: int) -> Event | None:
        cursor = await self.db.conn.execute(
            """SELECT * FROM events WHERE dedupe_key = ?
               AND created_at > datetime('now', ?) AND status != 'expired'
               ORDER BY created_at DESC LIMIT 1""",
            (dedupe_key, f"-{window_seconds} seconds"),
        )
        row = await cursor.fetchone()
        return self._row_to_event(row) if row else None

    async def get_chain(self, event_id: str) -> list[Event]:
        """Get full chain: walk up to root, then get all descendants."""
        # Find root
        root_id = event_id
        visited = {root_id}
        while True:
            cursor = await self.db.conn.execute(
                "SELECT parent_event FROM events WHERE id = ?", (root_id,)
            )
            row = await cursor.fetchone()
            if not row or not row["parent_event"]:
                break
            root_id = row["parent_event"]
            if root_id in visited:
                break
            visited.add(root_id)

        # Get all descendants from root
        result = []
        queue = [root_id]
        seen = set()
        while queue:
            current_id = queue.pop(0)
            if current_id in seen:
                continue
            seen.add(current_id)
            event = await self.get(current_id)
            if event:
                result.append(event)
            cursor = await self.db.conn.execute(
                "SELECT id FROM events WHERE parent_event = ?", (current_id,)
            )
            children = await cursor.fetchall()
            queue.extend(r["id"] for r in children)
        return result

    async def count_descendants(self, root_event_id: str) -> int:
        """Count all descendants of a root event for chain budget."""
        count = 0
        queue = [root_event_id]
        seen = set()
        while queue:
            current_id = queue.pop(0)
            if current_id in seen:
                continue
            seen.add(current_id)
            cursor = await self.db.conn.execute(
                "SELECT id FROM events WHERE parent_event = ?", (current_id,)
            )
            children = await cursor.fetchall()
            for r in children:
                count += 1
                queue.append(r["id"])
        return count

    async def get_chain_depth(self, event_id: str) -> int:
        """Walk up from event to root, counting depth."""
        depth = 0
        current = event_id
        visited = set()
        while current:
            if current in visited:
                break
            visited.add(current)
            cursor = await self.db.conn.execute(
                "SELECT parent_event FROM events WHERE id = ?", (current,)
            )
            row = await cursor.fetchone()
            if not row or not row["parent_event"]:
                break
            current = row["parent_event"]
            depth += 1
        return depth

    def _row_to_event(self, row) -> Event:
        return Event(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            topic=row["topic"],
            payload=json.loads(row["payload"]),
            priority=row["priority"],
            semantic_type=row["semantic_type"],
            dedupe_key=row["dedupe_key"],
            dedupe_count=row["dedupe_count"],
            parent_event=row["parent_event"],
            output_topic=row["output_topic"],
            context_refs=json.loads(row["context_refs"] or "[]"),
            memory_scope=row["memory_scope"],
            source=row["source"],
            status=row["status"],
            producer_id=row["producer_id"],
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            max_retries=row["max_retries"] or 0,
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# --- Agents ---

class AgentRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, data: AgentCreate) -> Agent:
        agent_id = f"agent_{_slug(data.name)}"
        now = _now_iso()
        agent = Agent(
            id=agent_id,
            name=data.name,
            model=data.model,
            system_prompt=data.system_prompt,
            description=data.description,
            fallback_model=data.fallback_model,
            capabilities=data.capabilities,
            max_concurrent=data.max_concurrent,
            queue_size=data.queue_size,
            memory_scope=data.memory_scope,
            config=data.config,
        )
        await self.db.conn.execute(
            """INSERT INTO agents (id, name, model, system_prompt, description,
               fallback_model, capabilities, max_concurrent, queue_size,
               memory_scope, config, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent.id, agent.name, agent.model, agent.system_prompt,
                agent.description, agent.fallback_model,
                json.dumps(agent.capabilities), agent.max_concurrent,
                agent.queue_size, agent.memory_scope, json.dumps(agent.config),
                agent.status.value, now, now,
            ),
        )
        await self.db.conn.commit()
        return agent

    async def get(self, agent_id: str) -> Agent | None:
        cursor = await self.db.conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def list(self) -> list[Agent]:
        cursor = await self.db.conn.execute(
            "SELECT * FROM agents ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def update(self, agent_id: str, data: dict) -> Agent | None:
        if "capabilities" in data:
            data["capabilities"] = json.dumps(data["capabilities"])
        if "config" in data:
            data["config"] = json.dumps(data["config"])
        data["updated_at"] = _now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in data)
        values = list(data.values()) + [agent_id]
        await self.db.conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = ?", values
        )
        await self.db.conn.commit()
        return await self.get(agent_id)

    async def delete(self, agent_id: str) -> bool:
        cursor = await self.db.conn.execute(
            "DELETE FROM agents WHERE id = ?", (agent_id,)
        )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    async def update_status(self, agent_id: str, status: str) -> None:
        await self.db.conn.execute(
            "UPDATE agents SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), agent_id),
        )
        await self.db.conn.commit()

    def _row_to_agent(self, row) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            model=row["model"],
            system_prompt=row["system_prompt"],
            description=row["description"],
            fallback_model=row["fallback_model"],
            capabilities=json.loads(row["capabilities"] or "[]"),
            max_concurrent=row["max_concurrent"],
            queue_size=row["queue_size"],
            memory_scope=row["memory_scope"],
            config=json.loads(row["config"] or "{}"),
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


# --- Routing Rules ---

class RoutingRuleRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, data: RoutingRuleCreate) -> RoutingRule:
        rule_id = f"rule_{uuid4().hex[:8]}"
        rule = RoutingRule(
            id=rule_id,
            name=data.name,
            topic_pattern=data.topic_pattern,
            semantic_type_pattern=data.semantic_type_pattern,
            min_priority=data.min_priority,
            required_capabilities=data.required_capabilities,
            consumer_id=data.consumer_id,
            model_override=data.model_override,
            token_budget_override=data.token_budget_override,
            priority_order=data.priority_order,
            enabled=data.enabled,
        )
        await self.db.conn.execute(
            """INSERT INTO routing_rules (id, name, topic_pattern, semantic_type_pattern,
               min_priority, required_capabilities, consumer_id, model_override,
               token_budget_override, priority_order, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule.id, rule.name, rule.topic_pattern, rule.semantic_type_pattern,
                rule.min_priority.value if rule.min_priority else None,
                json.dumps(rule.required_capabilities), rule.consumer_id,
                rule.model_override, rule.token_budget_override,
                rule.priority_order, int(rule.enabled), _now_iso(),
            ),
        )
        await self.db.conn.commit()
        return rule

    async def list(self, enabled_only: bool = False) -> list[RoutingRule]:
        query = "SELECT * FROM routing_rules"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY priority_order"
        cursor = await self.db.conn.execute(query)
        rows = await cursor.fetchall()
        return [self._row_to_rule(r) for r in rows]

    async def get(self, rule_id: str) -> RoutingRule | None:
        cursor = await self.db.conn.execute(
            "SELECT * FROM routing_rules WHERE id = ?", (rule_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_rule(row) if row else None

    async def delete(self, rule_id: str) -> bool:
        cursor = await self.db.conn.execute(
            "DELETE FROM routing_rules WHERE id = ?", (rule_id,)
        )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    def _row_to_rule(self, row) -> RoutingRule:
        return RoutingRule(
            id=row["id"],
            name=row["name"],
            topic_pattern=row["topic_pattern"],
            semantic_type_pattern=row["semantic_type_pattern"],
            min_priority=row["min_priority"],
            required_capabilities=json.loads(row["required_capabilities"] or "[]"),
            consumer_id=row["consumer_id"],
            model_override=row["model_override"],
            token_budget_override=row["token_budget_override"],
            priority_order=row["priority_order"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# --- Assignments ---

class AssignmentRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, event_id: str, agent_id: str, model_used: str | None = None, token_budget: int | None = None) -> EventAssignment:
        assignment_id = f"assign_{uuid4().hex[:10]}"
        assignment = EventAssignment(
            id=assignment_id,
            event_id=event_id,
            agent_id=agent_id,
            model_used=model_used,
            token_budget=token_budget,
        )
        await self.db.conn.execute(
            """INSERT INTO event_assignments (id, event_id, agent_id, status,
               retry_count, model_used, token_budget, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                assignment.id, assignment.event_id, assignment.agent_id,
                assignment.status.value, 0, model_used, token_budget, _now_iso(),
            ),
        )
        await self.db.conn.commit()
        return assignment

    async def claim_next(self, agent_id: str) -> EventAssignment | None:
        """Atomically claim the next pending assignment for an agent.

        Uses UPDATE ... WHERE status = 'pending' RETURNING to prevent double-claims.
        Falls back to UPDATE-then-SELECT if RETURNING is not supported.
        """
        now = _now_iso()
        # Atomic: UPDATE with status predicate — only succeeds if still pending
        cursor = await self.db.conn.execute(
            """UPDATE event_assignments
               SET status = 'claimed', started_at = ?
               WHERE id = (
                   SELECT id FROM event_assignments
                   WHERE agent_id = ? AND status = 'pending'
                   ORDER BY created_at ASC LIMIT 1
               ) AND status = 'pending'""",
            (now, agent_id),
        )
        if cursor.rowcount == 0:
            return None
        await self.db.conn.commit()

        # Fetch the claimed row
        cursor = await self.db.conn.execute(
            """SELECT * FROM event_assignments
               WHERE agent_id = ? AND status = 'claimed' AND started_at = ?
               ORDER BY created_at ASC LIMIT 1""",
            (agent_id, now),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_assignment(row)

    async def update_status(self, assignment_id: str, status: AssignmentStatus, error_message: str | None = None) -> None:
        updates = {"status": status.value}
        if status == AssignmentStatus.completed:
            updates["completed_at"] = _now_iso()
        if error_message:
            updates["error_message"] = error_message
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [assignment_id]
        await self.db.conn.execute(
            f"UPDATE event_assignments SET {set_clause} WHERE id = ?", values
        )
        await self.db.conn.commit()

    async def get_for_event(self, event_id: str) -> list[EventAssignment]:
        cursor = await self.db.conn.execute(
            "SELECT * FROM event_assignments WHERE event_id = ? ORDER BY created_at",
            (event_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_assignment(r) for r in rows]

    async def get_pending_count(self, agent_id: str) -> int:
        cursor = await self.db.conn.execute(
            "SELECT COUNT(*) as cnt FROM event_assignments WHERE agent_id = ? AND status IN ('pending', 'claimed', 'running')",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def exists(self, event_id: str, agent_id: str) -> bool:
        cursor = await self.db.conn.execute(
            "SELECT 1 FROM event_assignments WHERE event_id = ? AND agent_id = ?",
            (event_id, agent_id),
        )
        return await cursor.fetchone() is not None

    def _row_to_assignment(self, row) -> EventAssignment:
        return EventAssignment(
            id=row["id"],
            event_id=row["event_id"],
            agent_id=row["agent_id"],
            status=row["status"],
            retry_count=row["retry_count"],
            model_used=row["model_used"],
            token_budget=row["token_budget"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# --- Memory ---

class MemoryRepository:
    def __init__(self, db: Database):
        self.db = db

    async def append(self, entry: MemoryEntry) -> None:
        await self.db.conn.execute(
            """INSERT INTO agent_memory (agent_id, memory_scope, role, content, event_id, token_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entry.agent_id, entry.memory_scope, entry.role, entry.content,
             entry.event_id, entry.token_count, _now_iso()),
        )
        await self.db.conn.commit()

    async def get_recent(self, agent_id: str, scope: str, limit: int = 20) -> list[MemoryEntry]:
        cursor = await self.db.conn.execute(
            """SELECT * FROM agent_memory
               WHERE agent_id = ? AND memory_scope = ?
               ORDER BY created_at DESC LIMIT ?""",
            (agent_id, scope, limit),
        )
        rows = await cursor.fetchall()
        return [
            MemoryEntry(
                id=r["id"], agent_id=r["agent_id"], memory_scope=r["memory_scope"],
                role=r["role"], content=r["content"], event_id=r["event_id"],
                token_count=r["token_count"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in reversed(rows)  # Oldest first
        ]

    async def clear(self, agent_id: str, scope: str) -> None:
        await self.db.conn.execute(
            "DELETE FROM agent_memory WHERE agent_id = ? AND memory_scope = ?",
            (agent_id, scope),
        )
        await self.db.conn.commit()

    async def get_pinned_facts(self, agent_id: str, scope: str) -> list[PinnedFact]:
        cursor = await self.db.conn.execute(
            "SELECT * FROM agent_pinned_facts WHERE agent_id = ? AND memory_scope = ?",
            (agent_id, scope),
        )
        rows = await cursor.fetchall()
        return [
            PinnedFact(
                id=r["id"], agent_id=r["agent_id"], memory_scope=r["memory_scope"],
                content=r["content"], created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def add_pinned_fact(self, agent_id: str, scope: str, content: str) -> None:
        await self.db.conn.execute(
            "INSERT INTO agent_pinned_facts (agent_id, memory_scope, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, scope, content, _now_iso()),
        )
        await self.db.conn.commit()


# --- Agent Responses ---

class ResponseRepository:
    def __init__(self, db: Database):
        self.db = db

    async def create(self, response: AgentResponse) -> AgentResponse:
        await self.db.conn.execute(
            """INSERT INTO agent_responses (id, assignment_id, agent_id, event_id,
               response_text, parsed_output, output_event_id, model_used,
               tokens_used, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                response.id, response.assignment_id, response.agent_id,
                response.event_id, response.response_text,
                json.dumps(response.parsed_output.model_dump()) if response.parsed_output else None,
                response.output_event_id, response.model_used,
                response.tokens_used, response.duration_ms, _now_iso(),
            ),
        )
        await self.db.conn.commit()
        return response

    async def get_for_event(self, event_id: str) -> list[AgentResponse]:
        cursor = await self.db.conn.execute(
            "SELECT * FROM agent_responses WHERE event_id = ? ORDER BY created_at",
            (event_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_response(r) for r in rows]

    def _row_to_response(self, row) -> AgentResponse:
        from aiventbus.models import AgentResponseOutput
        parsed = None
        if row["parsed_output"]:
            try:
                parsed = AgentResponseOutput(**json.loads(row["parsed_output"]))
            except Exception:
                pass
        return AgentResponse(
            id=row["id"],
            assignment_id=row["assignment_id"],
            agent_id=row["agent_id"],
            event_id=row["event_id"],
            response_text=row["response_text"],
            parsed_output=parsed,
            output_event_id=row["output_event_id"],
            model_used=row["model_used"],
            tokens_used=row["tokens_used"],
            duration_ms=row["duration_ms"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
