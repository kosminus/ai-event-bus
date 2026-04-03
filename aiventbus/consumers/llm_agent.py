"""LLM Agent Consumer — pulls assignments, calls Ollama, parses output.

Each agent runs as a long-lived asyncio task that:
1. Waits for notification that work is available
2. Claims a pending assignment
3. Builds context via ContextEngine
4. Calls Ollama streaming
5. Parses structured output
6. Handles chain reactions (emit_event actions)
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from aiventbus.ai.context_engine import ContextEngine
from aiventbus.ai.ollama_client import OllamaClient
from aiventbus.ai.output_parser import OutputParser
from aiventbus.consumers.base import BaseConsumer
from aiventbus.core.bus import EventBus, WebSocketHub
from aiventbus.core.executor import Executor
from aiventbus.core.policy import PolicyEngine
from aiventbus.models import (
    ActionStatus,
    Agent,
    AgentResponse,
    AgentStatus,
    AssignmentStatus,
    EventCreate,
    EventStatus,
    MemoryEntry,
    PendingAction,
    TrustMode,
)
from aiventbus.storage.repositories import (
    AgentRepository,
    AssignmentRepository,
    EventRepository,
    MemoryRepository,
    PendingActionRepository,
    ResponseRepository,
)

logger = logging.getLogger(__name__)


class LLMAgentConsumer(BaseConsumer):
    """An Ollama-backed LLM agent that consumes events."""

    def __init__(
        self,
        agent: Agent,
        bus: EventBus,
        ollama: OllamaClient,
        context_engine: ContextEngine,
        output_parser: OutputParser,
        event_repo: EventRepository,
        agent_repo: AgentRepository,
        assignment_repo: AssignmentRepository,
        memory_repo: MemoryRepository,
        response_repo: ResponseRepository,
        ws_hub: WebSocketHub,
        policy_engine: PolicyEngine | None = None,
        executor: Executor | None = None,
        action_repo: PendingActionRepository | None = None,
    ):
        self.agent = agent
        self.bus = bus
        self.ollama = ollama
        self.context_engine = context_engine
        self.output_parser = output_parser
        self.event_repo = event_repo
        self.agent_repo = agent_repo
        self.assignment_repo = assignment_repo
        self.memory_repo = memory_repo
        self.response_repo = response_repo
        self.ws_hub = ws_hub
        self.policy_engine = policy_engine
        self.executor = executor
        self.action_repo = action_repo

        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._running = False
        self._semaphore = asyncio.Semaphore(agent.max_concurrent)

    async def start(self) -> None:
        """Start the agent worker loop."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Agent %s started (model=%s)", self.agent.id, self.agent.model)

    async def stop(self) -> None:
        """Stop the agent worker loop."""
        self._running = False
        self._wake_event.set()  # Wake it up so it can exit
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Agent %s stopped", self.agent.id)

    async def notify(self) -> None:
        """Notify the agent that new work is available."""
        self._wake_event.set()

    async def _run_loop(self) -> None:
        """Main worker loop — wait for work, claim, spawn concurrent tasks."""
        while self._running:
            try:
                # Wait for notification
                await self._wake_event.wait()
                self._wake_event.clear()

                if not self._running:
                    break

                # Claim and spawn tasks up to max_concurrent
                while self._running:
                    # Acquire semaphore before claiming to respect concurrency limit
                    await self._semaphore.acquire()

                    # If this is the last slot, reserve it for interactive work
                    if self.agent.max_concurrent > 1 and self._semaphore._value == 0:
                        assignment = await self.assignment_repo.claim_next(self.agent.id, lane_filter="interactive")
                    else:
                        assignment = await self.assignment_repo.claim_next(self.agent.id)

                    if not assignment:
                        self._semaphore.release()
                        break  # No more work, go back to waiting

                    # Spawn concurrent task — semaphore released when done
                    asyncio.create_task(self._process_with_semaphore(assignment))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Agent %s loop error: %s", self.agent.id, e)
                await asyncio.sleep(1)  # Brief pause before retrying

    async def _process_with_semaphore(self, assignment) -> None:
        """Process assignment and release semaphore when done."""
        try:
            await self._process_assignment(assignment)
        finally:
            self._semaphore.release()

    async def _process_assignment(self, assignment) -> None:
        """Process a single assignment: context → Ollama → parse → actions."""
        event = await self.event_repo.get(assignment.event_id)
        if not event:
            logger.error("Event %s not found for assignment %s", assignment.event_id, assignment.id)
            await self.assignment_repo.update_status(assignment.id, AssignmentStatus.failed, "Event not found")
            return

        # Update statuses (use bus for event status to trigger WebSocket broadcast)
        await self.assignment_repo.update_status(assignment.id, AssignmentStatus.running)
        await self.agent_repo.update_status(self.agent.id, AgentStatus.processing.value)
        await self.bus.update_event_status(event.id, EventStatus.processing)

        # Broadcast status change
        await self.ws_hub.broadcast(
            f"agents:{self.agent.id}", "agent.status",
            {"agent_id": self.agent.id, "status": "processing", "event_id": event.id},
        )

        start_time = time.monotonic()

        try:
            # 1. Build context
            messages = await self.context_engine.build_prompt(event, self.agent, assignment)

            # 2. Resolve model
            model = self._resolve_model(assignment)

            # 3. Call Ollama (streaming)
            response_chunks: list[str] = []
            async for chunk in self.ollama.chat(model, messages):
                response_chunks.append(chunk)
                # Stream tokens to WebSocket
                await self.ws_hub.broadcast(
                    f"agents:{self.agent.id}", "agent.stream",
                    {"agent_id": self.agent.id, "event_id": event.id, "token": chunk},
                )

            full_response = "".join(response_chunks)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # 4. Parse structured output
            parsed = self.output_parser.parse(full_response)

            # 5. Store memory
            scope = event.memory_scope or self.agent.memory_scope or self.agent.id
            event_prompt = messages[-1]["content"] if messages else ""
            await self.memory_repo.append(MemoryEntry(
                agent_id=self.agent.id,
                memory_scope=scope,
                role="user",
                content=event_prompt,
                event_id=event.id,
                token_count=len(event_prompt) // 4,
            ))
            await self.memory_repo.append(MemoryEntry(
                agent_id=self.agent.id,
                memory_scope=scope,
                role="assistant",
                content=full_response,
                event_id=event.id,
                token_count=len(full_response) // 4,
            ))

            # 6. Store response
            response_id = f"resp_{uuid4().hex[:10]}"
            agent_response = AgentResponse(
                id=response_id,
                assignment_id=assignment.id,
                agent_id=self.agent.id,
                event_id=event.id,
                response_text=full_response,
                parsed_output=parsed,
                model_used=model,
                tokens_used=len(full_response) // 4,
                duration_ms=duration_ms,
            )
            await self.response_repo.create(agent_response)

            # 7. Handle parse failure
            if parsed is None:
                logger.warning(
                    "Agent %s returned unparseable output for event %s",
                    self.agent.id, event.id,
                )
                await self.bus._emit_system_event("system.parse_failure", {
                    "agent_id": self.agent.id,
                    "event_id": event.id,
                    "response_preview": full_response[:500],
                })

            # 8. Handle chain reactions (proposed actions)
            if parsed and parsed.proposed_actions:
                for action in parsed.proposed_actions:
                    await self._execute_action(action, event, agent_response)

            # 9. Mark complete
            await self.assignment_repo.update_status(assignment.id, AssignmentStatus.completed)
            await self.bus.update_event_status(event.id, EventStatus.completed)

            # Broadcast completion
            await self.ws_hub.broadcast(
                f"agents:{self.agent.id}", "agent.response",
                {
                    "agent_id": self.agent.id,
                    "event_id": event.id,
                    "response_id": response_id,
                    "summary": parsed.summary if parsed else full_response[:200],
                    "duration_ms": duration_ms,
                },
            )

            logger.info(
                "Agent %s completed event %s in %dms (model=%s)",
                self.agent.id, event.id, duration_ms, model,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error("Agent %s failed on event %s: %s", self.agent.id, event.id, e)

            await self.assignment_repo.update_status(
                assignment.id, AssignmentStatus.failed, str(e)
            )
            await self.bus.update_event_status(event.id, EventStatus.failed)

            await self.bus._emit_system_event("system.agent_failure", {
                "agent_id": self.agent.id,
                "event_id": event.id,
                "error": str(e),
                "duration_ms": duration_ms,
            })

        finally:
            # Check if there's more work, otherwise go idle
            pending = await self.assignment_repo.get_pending_count(self.agent.id)
            if pending == 0:
                await self.agent_repo.update_status(self.agent.id, AgentStatus.idle.value)
                await self.ws_hub.broadcast(
                    f"agents:{self.agent.id}", "agent.status",
                    {"agent_id": self.agent.id, "status": "idle"},
                )

    def _resolve_model(self, assignment) -> str:
        """Resolve which model to use. Priority: assignment override → agent default."""
        if assignment.model_used:
            return assignment.model_used
        return self.agent.model

    async def _execute_action(self, action: dict, source_event, response) -> None:
        """Execute a proposed action through the policy engine and executor."""
        action_type = action.get("action_type")

        # Handle built-in bus actions directly (always auto-trusted)
        if action_type == "emit_event":
            topic = action.get("topic") or source_event.output_topic
            if not topic:
                logger.warning("emit_event action has no topic, skipping")
                return
            payload = action.get("payload", {})
            await self.bus.publish(
                EventCreate(
                    topic=topic,
                    payload=payload,
                    parent_event=source_event.id,
                    memory_scope=source_event.memory_scope,
                    source=f"agent:{self.agent.id}",
                ),
            )
            logger.info("Chain reaction: agent %s emitted event on %s", self.agent.id, topic)
            return

        if action_type == "log":
            message = action.get("message", "")
            logger.info("Agent %s log: %s", self.agent.id, message)
            return

        if action_type == "alert":
            message = action.get("message", "")
            logger.warning("Agent %s ALERT: %s", self.agent.id, message)
            await self.ws_hub.broadcast(
                "system", "system.alert",
                {"agent_id": self.agent.id, "message": message},
            )
            return

        # All other actions go through policy engine → executor
        if not self.policy_engine or not self.executor:
            logger.warning("Agent %s proposed %s but policy/executor not configured", self.agent.id, action_type)
            return

        decision = self.policy_engine.evaluate(action_type, action)

        if decision.trust_mode == TrustMode.deny:
            logger.warning(
                "DENIED action %s from agent %s: %s",
                action_type, self.agent.id, decision.reason,
            )
            await self.bus._emit_system_event("system.action_denied", {
                "agent_id": self.agent.id,
                "event_id": source_event.id,
                "action_type": action_type,
                "reason": decision.reason,
            })
            return

        if decision.trust_mode == TrustMode.auto:
            result = await self.executor.execute(action_type, action)
            logger.info("Auto-executed %s for agent %s: %s", action_type, self.agent.id, result)
            # Store and broadcast auto-executed actions so they appear in history/UI
            if self.action_repo:
                auto_action = PendingAction(
                    assignment_id=response.assignment_id if hasattr(response, "assignment_id") else "",
                    agent_id=self.agent.id,
                    event_id=source_event.id,
                    action_type=action_type,
                    action_data=action,
                    trust_mode=TrustMode.auto,
                    status=ActionStatus.completed,
                    policy_reason=decision.reason,
                )
                await self.action_repo.create(auto_action)
                await self.action_repo.update_result(auto_action.id, ActionStatus.completed, result)
            await self.ws_hub.broadcast("system", "action.executed", {
                "action_id": auto_action.id if self.action_repo else None,
                "agent_id": self.agent.id,
                "action_type": action_type,
                "action_data": action,
                "result": result,
            })
            return

        # TrustMode.confirm — queue for user approval
        if self.action_repo:
            pending = PendingAction(
                assignment_id=response.assignment_id if hasattr(response, "assignment_id") else "",
                agent_id=self.agent.id,
                event_id=source_event.id,
                action_type=action_type,
                action_data=action,
                trust_mode=TrustMode.confirm,
                status=ActionStatus.waiting_confirmation,
                policy_reason=decision.reason,
            )
            await self.action_repo.create(pending)
            logger.info("Action %s from agent %s queued for confirmation: %s", action_type, self.agent.id, pending.id)
            await self.ws_hub.broadcast("system", "action.pending", {
                "action_id": pending.id,
                "agent_id": self.agent.id,
                "action_type": action_type,
                "action_data": action,
            })
        else:
            logger.warning("Agent %s proposed %s requiring confirmation but no action_repo configured", self.agent.id, action_type)
