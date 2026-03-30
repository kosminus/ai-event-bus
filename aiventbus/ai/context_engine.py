"""Context Engine — the most AI-native component.

Resolves context refs, loads memory, assembles token-bounded prompts.
Priority: system prompt → pinned facts → recent memory → related events → current event.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiventbus.models import Agent, Event, EventAssignment
from aiventbus.storage.repositories import EventRepository, MemoryRepository

logger = logging.getLogger(__name__)

# Rough token estimation: ~4 chars per token
CHARS_PER_TOKEN = 4
DEFAULT_TOKEN_BUDGET = 4096


class ContextEngine:
    """Builds prompts for LLM agents with relevant context."""

    def __init__(self, event_repo: EventRepository, memory_repo: MemoryRepository):
        self.event_repo = event_repo
        self.memory_repo = memory_repo

    async def build_prompt(
        self,
        event: Event,
        agent: Agent,
        assignment: EventAssignment,
    ) -> list[dict[str, str]]:
        """Build a messages list for Ollama chat, respecting token budget.

        Returns list of {role, content} dicts.
        """
        token_budget = assignment.token_budget or DEFAULT_TOKEN_BUDGET
        messages: list[dict[str, str]] = []
        used_tokens = 0

        # 1. System prompt (always included)
        system_content = self._build_system_prompt(agent)
        messages.append({"role": "system", "content": system_content})
        used_tokens += self._estimate_tokens(system_content)

        # 2. Pinned facts
        scope = event.memory_scope or agent.memory_scope or agent.id
        pinned = await self.memory_repo.get_pinned_facts(agent.id, scope)
        if pinned:
            facts_text = "## Pinned Facts\n" + "\n".join(f"- {p.content}" for p in pinned)
            facts_tokens = self._estimate_tokens(facts_text)
            if used_tokens + facts_tokens < token_budget * 0.3:  # Max 30% for facts
                messages.append({"role": "system", "content": facts_text})
                used_tokens += facts_tokens

        # 3. Recent memory (conversation history)
        memory = await self.memory_repo.get_recent(agent.id, scope, limit=20)
        memory_budget = int(token_budget * 0.3)  # Max 30% for memory
        memory_tokens = 0
        memory_messages = []
        for entry in reversed(memory):  # Most recent first for budget trimming
            entry_tokens = self._estimate_tokens(entry.content)
            if memory_tokens + entry_tokens > memory_budget:
                break
            memory_messages.insert(0, {"role": entry.role, "content": entry.content})
            memory_tokens += entry_tokens
        messages.extend(memory_messages)
        used_tokens += memory_tokens

        # 4. Related events (from context_refs)
        if event.context_refs:
            related_text = await self._resolve_context_refs(event.context_refs)
            if related_text:
                ref_tokens = self._estimate_tokens(related_text)
                if used_tokens + ref_tokens < token_budget * 0.8:  # Leave room for event
                    messages.append({"role": "system", "content": related_text})
                    used_tokens += ref_tokens

        # 5. Current event (always included, this is the main prompt)
        event_prompt = self._format_event_prompt(event)
        messages.append({"role": "user", "content": event_prompt})

        return messages

    def _build_system_prompt(self, agent: Agent) -> str:
        """Build the agent's system prompt with bus context."""
        parts = [agent.system_prompt]
        parts.append(
            "\n\nYou are an AI agent connected to the AI Event Bus. "
            "You receive events and must respond with structured JSON output.\n\n"
            "Your response MUST be valid JSON with this structure:\n"
            "```json\n"
            '{\n'
            '  "type": "analysis" | "action" | "escalate",\n'
            '  "summary": "brief description of your analysis",\n'
            '  "confidence": 0.0 to 1.0,\n'
            '  "proposed_actions": [\n'
            '    {\n'
            '      "action_type": "emit_event" | "log" | "alert",\n'
            '      "topic": "optional.topic.for.emit_event",\n'
            '      "payload": {},\n'
            '      "message": "optional message for log/alert"\n'
            '    }\n'
            '  ]\n'
            '}\n'
            "```\n"
            "Respond ONLY with the JSON object, no additional text."
        )
        if agent.capabilities:
            parts.append(f"\nYour capabilities: {', '.join(agent.capabilities)}")
        return "\n".join(parts)

    def _format_event_prompt(self, event: Event) -> str:
        """Format an event as a structured prompt for the LLM."""
        lines = [
            "## Event",
            f"**Topic:** {event.topic}",
        ]
        if event.semantic_type:
            lines.append(f"**Semantic Type:** {event.semantic_type}")
        lines.append(f"**Priority:** {event.priority.value}")
        if event.context_refs:
            lines.append(f"**References:** {', '.join(event.context_refs)}")
        if event.dedupe_count > 1:
            lines.append(f"**Occurrences:** {event.dedupe_count} (deduped)")

        lines.append("\n**Payload:**")
        lines.append("```json")
        lines.append(json.dumps(event.payload, indent=2))
        lines.append("```")

        if event.output_topic:
            lines.append(f"\n**Output Topic:** {event.output_topic}")
            lines.append("If you propose an emit_event action, use this topic.")

        lines.append("\nAnalyze this event and respond with structured JSON.")
        return "\n".join(lines)

    async def _resolve_context_refs(self, refs: list[str]) -> str | None:
        """Resolve context references to actual event data."""
        resolved = []
        for ref in refs[:5]:  # Limit to 5 refs
            # Try to find event by ID or by pattern
            event = await self.event_repo.get(ref)
            if event:
                resolved.append(
                    f"**Ref {ref}:** topic={event.topic}, "
                    f"payload={json.dumps(event.payload)[:200]}"
                )
            else:
                resolved.append(f"**Ref {ref}:** (unresolved)")

        if not resolved:
            return None
        return "## Related Context\n" + "\n".join(resolved)

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate."""
        return len(text) // CHARS_PER_TOKEN
