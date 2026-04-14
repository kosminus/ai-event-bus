"""LLM Agent Consumer — tool-use loop over assignments.

Each agent runs as a long-lived asyncio task that:
1. Claims a pending or resumable assignment
2. On fresh work: builds prompt via ContextEngine
3. On resume: restores conversation state from assignment.conversation and
   pulls the resolved action result (approved or denied)
4. Runs a propose → execute → feed-result loop:
   - Streams LLM output and parses structured JSON
   - For each proposed action: auto-executes built-ins (emit_event/log/alert),
     routes the rest through PolicyEngine → Executor, and suspends when a
     confirm-gated action is queued to the user
   - Appends action results as a user turn and loops again
5. Terminates when the LLM returns no further actions (final summary) or the
   iteration cap is hit
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal
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

DEFAULT_MAX_ITERATIONS = 5


@dataclass
class ActionOutcome:
    """Result of attempting to execute a proposed action."""
    kind: Literal["executed", "denied", "waiting", "unknown"]
    action: dict
    result: dict | None = None
    action_id: str | None = None
    reason: str | None = None


class LLMAgentConsumer(BaseConsumer):
    """An Ollama-backed LLM agent that consumes events via a tool-use loop."""

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
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Agent %s started (model=%s)", self.agent.id, self.agent.model)

    async def stop(self) -> None:
        self._running = False
        self._wake_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Agent %s stopped", self.agent.id)

    async def notify(self) -> None:
        self._wake_event.set()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._wake_event.wait()
                self._wake_event.clear()
                if not self._running:
                    break
                while self._running:
                    await self._semaphore.acquire()
                    if self.agent.max_concurrent > 1 and self._semaphore._value == 0:
                        assignment = await self.assignment_repo.claim_next(self.agent.id, lane_filter="interactive")
                    else:
                        assignment = await self.assignment_repo.claim_next(self.agent.id)
                    if not assignment:
                        self._semaphore.release()
                        break
                    asyncio.create_task(self._process_with_semaphore(assignment))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Agent %s loop error: %s", self.agent.id, e)
                await asyncio.sleep(1)

    async def _process_with_semaphore(self, assignment) -> None:
        try:
            await self._process_assignment(assignment)
        finally:
            self._semaphore.release()

    async def _process_assignment(self, assignment) -> None:
        """Run the tool-use loop for an assignment (fresh or resumed)."""
        event = await self.event_repo.get(assignment.event_id)
        if not event:
            logger.error("Event %s not found for assignment %s", assignment.event_id, assignment.id)
            await self.assignment_repo.update_status(assignment.id, AssignmentStatus.failed, "Event not found")
            return

        await self.assignment_repo.update_status(assignment.id, AssignmentStatus.running)
        await self.agent_repo.update_status(self.agent.id, AgentStatus.processing.value)
        await self.bus.update_event_status(event.id, EventStatus.processing)
        await self.ws_hub.broadcast(
            f"agents:{self.agent.id}", "agent.status",
            {"agent_id": self.agent.id, "status": "processing", "event_id": event.id},
        )

        # Restore or initialise loop state
        state = await self._init_loop_state(assignment, event)

        scope = event.memory_scope or self.agent.memory_scope or self.agent.id
        max_iters = int(self.agent.config.get("max_tool_iterations", DEFAULT_MAX_ITERATIONS))
        model = self._resolve_model(assignment)

        try:
            # On fresh start, persist user turn to memory
            if assignment.iteration == 0 and not state.get("user_turn_persisted"):
                event_prompt = state["messages"][-1]["content"] if state["messages"] else ""
                await self.memory_repo.append(MemoryEntry(
                    agent_id=self.agent.id, memory_scope=scope, role="user",
                    content=event_prompt, event_id=event.id,
                    token_count=len(event_prompt) // 4,
                ))
                state["user_turn_persisted"] = True

            iteration = assignment.iteration
            final_summary: str | None = None
            final_response_text: str | None = None

            # Loop invariant: iteration counts completed tool batches. The LLM
            # always gets one more turn after the last batch to synthesize
            # results — we only gate *executing new actions* on the cap.
            while True:
                # If we were suspended mid-batch, pick up remaining actions without calling LLM
                if not state.get("remaining_actions") and not state.get("last_assistant_json"):
                    # Fresh iteration — call the LLM
                    start = time.monotonic()
                    raw = await self._stream_ollama(model, state["messages"], event)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    parsed = self.output_parser.parse(raw)
                    await self._persist_agent_response(assignment, event, raw, parsed, model, duration_ms)

                    if parsed is None:
                        await self.bus._emit_system_event("system.parse_failure", {
                            "agent_id": self.agent.id,
                            "event_id": event.id,
                            "response_preview": raw[:500],
                        })
                        final_response_text = raw
                        final_summary = raw[:200]
                        break

                    final_summary = parsed.summary
                    final_response_text = raw

                    actions = parsed.proposed_actions or []
                    if not actions:
                        # Terminal — final answer produced
                        break

                    if iteration >= max_iters:
                        # Cap reached: LLM saw the last batch's results and is
                        # still asking for more. Accept its summary as terminal.
                        logger.warning(
                            "Agent %s hit max_tool_iterations (%d) on event %s",
                            self.agent.id, max_iters, event.id,
                        )
                        await self.bus._emit_system_event("system.tool_loop_exhausted", {
                            "agent_id": self.agent.id,
                            "event_id": event.id,
                            "iterations": iteration,
                        })
                        final_summary = (final_summary or "") + " [iteration cap reached]"
                        break

                    state["last_assistant_json"] = raw
                    state["remaining_actions"] = list(actions)

                # Execute remaining actions, possibly suspending
                suspended = await self._execute_batch(assignment, event, state, iteration)
                if suspended:
                    # _execute_batch has persisted state and returned — assignment is now waiting
                    return

                # Batch complete — fold assistant + action results back into messages
                state["messages"].append({"role": "assistant", "content": state["last_assistant_json"] or ""})
                state["messages"].append({
                    "role": "user",
                    "content": self._format_action_results(state["partial_results"]),
                })
                state["last_assistant_json"] = None
                state["partial_results"] = []
                state["remaining_actions"] = []
                iteration += 1
                await self._persist_state(assignment, state, iteration, waiting_action_id=None, status=AssignmentStatus.running)

            # Persist assistant memory turn with final summary
            if final_response_text:
                await self.memory_repo.append(MemoryEntry(
                    agent_id=self.agent.id, memory_scope=scope, role="assistant",
                    content=final_response_text, event_id=event.id,
                    token_count=len(final_response_text) // 4,
                ))

            await self.assignment_repo.update_status(assignment.id, AssignmentStatus.completed)
            await self.bus.update_event_status(event.id, EventStatus.completed)
            await self.ws_hub.broadcast(
                f"agents:{self.agent.id}", "agent.response",
                {
                    "agent_id": self.agent.id,
                    "event_id": event.id,
                    "summary": final_summary or (final_response_text or "")[:200],
                },
            )
            logger.info("Agent %s completed event %s (iterations=%d)", self.agent.id, event.id, iteration)

        except Exception as e:
            logger.error("Agent %s failed on event %s: %s", self.agent.id, event.id, e)
            await self.assignment_repo.update_status(assignment.id, AssignmentStatus.failed, str(e))
            await self.bus.update_event_status(event.id, EventStatus.failed)
            await self.bus._emit_system_event("system.agent_failure", {
                "agent_id": self.agent.id,
                "event_id": event.id,
                "error": str(e),
            })
        finally:
            pending = await self.assignment_repo.get_pending_count(self.agent.id)
            if pending == 0:
                await self.agent_repo.update_status(self.agent.id, AgentStatus.idle.value)
                await self.ws_hub.broadcast(
                    f"agents:{self.agent.id}", "agent.status",
                    {"agent_id": self.agent.id, "status": "idle"},
                )

    async def _init_loop_state(self, assignment, event) -> dict:
        """Initialise or restore the in-memory loop state dict."""
        if assignment.conversation:
            state = dict(assignment.conversation)
            state.setdefault("messages", [])
            state.setdefault("partial_results", [])
            state.setdefault("remaining_actions", [])
            state.setdefault("last_assistant_json", None)
            # If we were waiting on a user-confirmed action, harvest its outcome
            if assignment.waiting_action_id and self.action_repo:
                action = await self.action_repo.get(assignment.waiting_action_id)
                if action:
                    outcome_result: dict
                    if action.status == ActionStatus.completed:
                        outcome_result = action.result or {}
                    elif action.status == ActionStatus.denied:
                        outcome_result = {"error": "denied by user", "reason": action.policy_reason}
                    else:
                        outcome_result = {"error": f"action ended with status {action.status.value if hasattr(action.status, 'value') else action.status}"}
                    merged_action = dict(action.action_data)
                    merged_action.setdefault("action_type", action.action_type)
                    state["partial_results"].append({
                        "action": merged_action,
                        "result": outcome_result,
                        "status": "executed" if action.status == ActionStatus.completed else "denied",
                    })
            return state

        # Fresh state
        messages = await self.context_engine.build_prompt(event, self.agent, assignment)
        return {
            "messages": messages,
            "partial_results": [],
            "remaining_actions": [],
            "last_assistant_json": None,
            "user_turn_persisted": False,
        }

    async def _stream_ollama(self, model: str, messages: list[dict], event) -> str:
        chunks: list[str] = []
        async for chunk in self.ollama.chat(model, messages):
            chunks.append(chunk)
            await self.ws_hub.broadcast(
                f"agents:{self.agent.id}", "agent.stream",
                {"agent_id": self.agent.id, "event_id": event.id, "token": chunk},
            )
        return "".join(chunks)

    async def _persist_agent_response(self, assignment, event, raw: str, parsed, model: str, duration_ms: int) -> AgentResponse:
        response = AgentResponse(
            id=f"resp_{uuid4().hex[:10]}",
            assignment_id=assignment.id,
            agent_id=self.agent.id,
            event_id=event.id,
            response_text=raw,
            parsed_output=parsed,
            model_used=model,
            tokens_used=len(raw) // 4,
            duration_ms=duration_ms,
        )
        await self.response_repo.create(response)
        return response

    async def _execute_batch(self, assignment, event, state: dict, iteration: int) -> bool:
        """Execute actions from state['remaining_actions'] in order.

        Returns True if the assignment was suspended (confirm-gated action queued).
        Returns False when the batch completed synchronously.
        """
        while state["remaining_actions"]:
            action = state["remaining_actions"].pop(0)
            outcome = await self._execute_action(action, event, assignment)

            if outcome.kind == "waiting":
                # Put the action back at the front so resume sees it was last-in-flight
                # (resume path harvests the pending_action result into partial_results directly)
                await self._persist_state(
                    assignment, state, iteration,
                    waiting_action_id=outcome.action_id,
                    status=AssignmentStatus.waiting_confirmation,
                )
                await self.ws_hub.broadcast(
                    f"agents:{self.agent.id}", "agent.status",
                    {"agent_id": self.agent.id, "status": "waiting_confirmation", "event_id": event.id},
                )
                return True

            state["partial_results"].append({
                "action": action,
                "result": outcome.result or ({"error": outcome.reason} if outcome.reason else {"ok": True}),
                "status": outcome.kind,
            })

        return False

    async def _persist_state(self, assignment, state: dict, iteration: int,
                             waiting_action_id: str | None, status: AssignmentStatus) -> None:
        """Save loop state to the assignment row."""
        # Strip keys we don't want to persist (e.g. user_turn_persisted is ephemeral)
        to_save = {
            "messages": state.get("messages", []),
            "partial_results": state.get("partial_results", []),
            "remaining_actions": state.get("remaining_actions", []),
            "last_assistant_json": state.get("last_assistant_json"),
            "user_turn_persisted": state.get("user_turn_persisted", False),
        }
        if status == AssignmentStatus.waiting_confirmation:
            await self.assignment_repo.suspend(assignment.id, to_save, iteration, waiting_action_id or "")
        else:
            # Just update conversation + iteration without changing status away from 'running'
            await self.assignment_repo.db.conn.execute(
                """UPDATE event_assignments
                   SET conversation = ?, iteration = ?, waiting_action_id = NULL
                   WHERE id = ?""",
                (json.dumps(to_save), iteration, assignment.id),
            )
            await self.assignment_repo.db.conn.commit()

    def _format_action_results(self, partial_results: list[dict]) -> str:
        """Render executed action results as a user-role message for the LLM."""
        lines = ["Here are the results of the actions you just proposed. "
                 "Use these to produce your next step. If no further actions are "
                 "needed, reply with a terminal summary and an empty proposed_actions list.\n"]
        for i, entry in enumerate(partial_results, start=1):
            action = entry.get("action", {})
            action_type = action.get("action_type", "unknown")
            result = entry.get("result", {})
            status = entry.get("status", "executed")
            lines.append(f"### Action {i}: {action_type} ({status})")
            lines.append("```json")
            try:
                lines.append(json.dumps(result, indent=2, default=str)[:4000])
            except Exception:
                lines.append(str(result)[:4000])
            lines.append("```")
        return "\n".join(lines)

    def _resolve_model(self, assignment) -> str:
        if assignment.model_used:
            return assignment.model_used
        return self.agent.model

    async def _execute_action(self, action: dict, source_event, assignment) -> ActionOutcome:
        """Execute a proposed action through built-in handlers, policy engine, or executor.

        Returns an ActionOutcome describing what happened — the caller folds it
        into the conversation as the result of this action.
        """
        action_type = action.get("action_type")

        # Built-in bus actions
        if action_type == "emit_event":
            topic = action.get("topic") or source_event.output_topic
            if not topic:
                logger.warning("emit_event action has no topic, skipping")
                return ActionOutcome(kind="denied", action=action, reason="emit_event missing topic")
            payload = action.get("payload", {})
            published = await self.bus.publish(EventCreate(
                topic=topic, payload=payload,
                parent_event=source_event.id,
                memory_scope=source_event.memory_scope,
                source=f"agent:{self.agent.id}",
            ))
            return ActionOutcome(kind="executed", action=action, result={"emitted_event_id": getattr(published, "id", None), "topic": topic})

        if action_type == "log":
            message = action.get("message", "")
            logger.info("Agent %s log: %s", self.agent.id, message)
            return ActionOutcome(kind="executed", action=action, result={"logged": message})

        if action_type == "alert":
            message = action.get("message", "")
            logger.warning("Agent %s ALERT: %s", self.agent.id, message)
            await self.ws_hub.broadcast("system", "system.alert",
                {"agent_id": self.agent.id, "message": message})
            return ActionOutcome(kind="executed", action=action, result={"alerted": message})

        if not self.policy_engine or not self.executor:
            logger.warning("Agent %s proposed %s but policy/executor not configured", self.agent.id, action_type)
            return ActionOutcome(kind="unknown", action=action, reason="policy/executor not configured")

        if not self.executor.has_handler(action_type):
            await self.bus._emit_system_event("system.unknown_action", {
                "agent_id": self.agent.id,
                "event_id": source_event.id,
                "action_type": action_type,
                "action_data": action,
            })
            await self.ws_hub.broadcast("system", "action.unknown", {
                "agent_id": self.agent.id,
                "event_id": source_event.id,
                "action_type": action_type,
            })
            return ActionOutcome(kind="unknown", action=action, reason=f"no handler for {action_type}")

        decision = self.policy_engine.evaluate(action_type, action)

        if decision.trust_mode == TrustMode.deny:
            await self.bus._emit_system_event("system.action_denied", {
                "agent_id": self.agent.id,
                "event_id": source_event.id,
                "action_type": action_type,
                "reason": decision.reason,
            })
            return ActionOutcome(kind="denied", action=action, reason=decision.reason)

        if decision.trust_mode == TrustMode.auto:
            result = await self.executor.execute(action_type, action)
            if self.action_repo:
                auto = PendingAction(
                    assignment_id=assignment.id,
                    agent_id=self.agent.id,
                    event_id=source_event.id,
                    action_type=action_type,
                    action_data=action,
                    trust_mode=TrustMode.auto,
                    status=ActionStatus.completed,
                    policy_reason=decision.reason,
                )
                await self.action_repo.create(auto)
                await self.action_repo.update_result(auto.id, ActionStatus.completed, result)
                await self.ws_hub.broadcast("system", "action.executed", {
                    "action_id": auto.id,
                    "agent_id": self.agent.id,
                    "action_type": action_type,
                    "action_data": action,
                    "result": result,
                })
            return ActionOutcome(kind="executed", action=action, result=result)

        # confirm — queue pending action, signal suspend
        if not self.action_repo:
            return ActionOutcome(kind="denied", action=action, reason="confirmation required but action_repo not configured")

        pending = PendingAction(
            assignment_id=assignment.id,
            agent_id=self.agent.id,
            event_id=source_event.id,
            action_type=action_type,
            action_data=action,
            trust_mode=TrustMode.confirm,
            status=ActionStatus.waiting_confirmation,
            policy_reason=decision.reason,
        )
        await self.action_repo.create(pending)
        await self.ws_hub.broadcast("system", "action.pending", {
            "action_id": pending.id,
            "agent_id": self.agent.id,
            "action_type": action_type,
            "action_data": action,
        })
        return ActionOutcome(kind="waiting", action=action, action_id=pending.id)
