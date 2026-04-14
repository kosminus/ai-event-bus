"""Context Engine — the most AI-native component.

Resolves context refs, loads memory, assembles token-bounded prompts.
Priority: system prompt → pinned facts → recent memory → related events → current event.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from aiventbus.models import Agent, Event, EventAssignment, KnowledgeEntry
from aiventbus.storage.repositories import EventRepository, KnowledgeRepository, MemoryRepository

if TYPE_CHECKING:
    from aiventbus.core.executor import Executor

logger = logging.getLogger(__name__)

# Rough token estimation: ~4 chars per token
CHARS_PER_TOKEN = 4
DEFAULT_TOKEN_BUDGET = 4096


class ContextEngine:
    """Builds prompts for LLM agents with relevant context."""

    def __init__(self, event_repo: EventRepository, memory_repo: MemoryRepository,
                 knowledge_repo: KnowledgeRepository | None = None,
                 executor: Executor | None = None):
        self.event_repo = event_repo
        self.memory_repo = memory_repo
        self.knowledge_repo = knowledge_repo
        self.executor = executor

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

        # 2.5. Knowledge store facts
        if self.knowledge_repo:
            knowledge = await self._get_relevant_knowledge(event, agent)
            if knowledge:
                knowledge_text = "## Known Facts\n" + "\n".join(
                    f"- **{k.key}**: {k.value}" for k in knowledge
                )
                knowledge_tokens = self._estimate_tokens(knowledge_text)
                if used_tokens + knowledge_tokens < token_budget * 0.2:
                    messages.append({"role": "system", "content": knowledge_text})
                    used_tokens += knowledge_tokens

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
        """Build the agent's system prompt with bus context.

        The available action types are generated dynamically from the executor
        so new tools (Playwright, MCP, http_request, etc.) appear automatically.
        """
        parts = [agent.system_prompt]

        # Build action type docs dynamically
        action_docs = self._build_action_docs()

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
            '      "action_type": "<one of the types listed below>",\n'
            '      "...": "action-specific parameters (see below)"\n'
            '    }\n'
            '  ]\n'
            '}\n'
            "```\n\n"
            "## Tool-use loop\n\n"
            "You operate in a loop: you propose actions, the bus executes them "
            "(after policy or user approval), and the results are fed back as a "
            "user-role message. You may then propose more actions or produce a "
            "final answer.\n\n"
            "- If an action's result gives you enough information to answer the "
            "user, respond with a final `summary` and set `proposed_actions` to "
            "`[]`. That terminates the loop and delivers your summary as the "
            "final answer.\n"
            "- If you still need more data, propose the next action(s). Do not "
            "re-propose the same action with the same params — read the prior "
            "result first.\n"
            "- Actions that require user confirmation will pause the loop until "
            "the user approves or denies; the denial reason is returned as the "
            "action result.\n\n"
            "## Available action types\n\n"
            "You may ONLY use the following action types. Do NOT invent new ones.\n\n"
            f"{action_docs}\n"
            "Respond ONLY with the JSON object, no additional text."
        )

        # Tool backend details
        tool_docs = self._build_tool_docs()
        if tool_docs:
            parts.append(f"\n## External tools\n\n{tool_docs}")

        if agent.capabilities:
            parts.append(f"\nYour capabilities: {', '.join(agent.capabilities)}")
        return "\n".join(parts)

    def _build_action_docs(self) -> str:
        """Generate action type documentation from the executor."""
        if not self.executor:
            # Fallback: minimal hardcoded list (shouldn't happen in practice)
            return (
                '- **emit_event**: Publish a new event. Params: topic, payload\n'
                '- **log**: Log a message. Params: message\n'
                '- **alert**: Broadcast an alert. Params: message\n'
                '- **notify**: Desktop notification. Params: title, message\n'
                '- **shell_exec**: Run a shell command. Params: command, cwd, timeout\n'
                '- **http_request**: HTTP request. Params: url, method, headers, body\n'
                '- **file_read**: Read a file. Params: path\n'
                '- **file_write**: Write a file. Params: path, content\n'
                '- **file_delete**: Delete a file. Params: path\n'
                '- **open_app**: Open URL/file. Params: target\n'
                '- **set_knowledge**: Store a fact. Params: key, value\n'
                '- **get_knowledge**: Retrieve a fact. Params: key or prefix\n'
            )

        lines = []
        for action in self.executor.list_available_actions():
            at = action["action_type"]
            desc = action.get("description", "")
            params = action.get("params", {})
            param_str = ", ".join(f"{k}: {v}" for k, v in params.items()) if params else ""
            line = f"- **{at}**: {desc}"
            if param_str:
                line += f". Params: {{{param_str}}}"
            lines.append(line)
        return "\n".join(lines)

    def _build_tool_docs(self) -> str:
        """Generate documentation for registered tool backends."""
        if not self.executor:
            return ""

        tools = self.executor.tool_registry.list_tools()
        if not tools:
            return ""

        lines = ["Use action_type `tool_call` to invoke these tools:\n"]
        for tool in tools:
            lines.append(f"### {tool.name}")
            lines.append(f"{tool.description}\n")
            for method in tool.methods:
                params = method.parameters
                param_str = ", ".join(f"{k}" for k in params) if params else "none"
                lines.append(f"- **{method.name}**({param_str}): {method.description}")
            lines.append("")
        return "\n".join(lines)

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

    async def _get_relevant_knowledge(self, event: Event, agent: Agent) -> list[KnowledgeEntry]:
        """Retrieve relevant knowledge entries for the current context."""
        entries: list[KnowledgeEntry] = []
        seen_keys: set[str] = set()

        async def _add_scan(prefix: str) -> None:
            for entry in await self.knowledge_repo.scan(prefix):
                if entry.key not in seen_keys:
                    seen_keys.add(entry.key)
                    entries.append(entry)

        # Always include system facts (small, always useful)
        await _add_scan("system.")

        # Topic-based: clipboard.text → scan clipboard.*
        topic_root = event.topic.split(".")[0]
        if topic_root != "system":
            await _add_scan(f"{topic_root}.")

        # Agent-specific knowledge
        await _add_scan(f"agent.{agent.id}.")

        # User preferences
        await _add_scan("user.")

        return entries

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate."""
        return len(text) // CHARS_PER_TOKEN
