"""Event classifier — LLM-based routing fallback for unmatched events.

When no static routing rule matches, the classifier uses a lightweight model
to decide which agent(s) should handle the event, or whether to drop it (no_op).
"""

from __future__ import annotations

import json
import logging

from aiventbus.ai.ollama_client import OllamaClient
from aiventbus.models import Agent, Event

logger = logging.getLogger(__name__)

_CLASSIFIER_PROMPT = """\
You are an event routing classifier for an AI Event Bus system.

Given an event and a list of available agents, decide where to route the event.

## Available Agents
{agents_block}

## Rules
- Route to the MOST relevant agent based on the event topic and payload.
- If multiple agents are relevant, list up to 2.
- If NO agent is relevant, respond with "no_op" — not every event needs processing.
- Respond ONLY with valid JSON, no additional text.

## Response Format
```json
{{
  "route_to": "agent_id" | ["agent_id_1", "agent_id_2"] | "no_op",
  "reason": "brief explanation"
}}
```

## Event
Topic: {topic}
Payload: {payload}

Respond with JSON only."""


class EventClassifier:
    """Classifies unmatched events to determine routing."""

    def __init__(
        self,
        ollama: OllamaClient,
        model: str = "gemma4:latest",
        timeout_seconds: int = 10,
    ):
        self.ollama = ollama
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def classify(
        self,
        event: Event,
        available_agents: list[Agent],
    ) -> ClassificationResult:
        """Classify an event and return routing decision."""
        if not available_agents:
            return ClassificationResult(route_to=[], reason="No agents available")

        agents_block = "\n".join(
            f"- **{a.id}**: {a.description or a.name} (capabilities: {', '.join(a.capabilities) or 'general'})"
            for a in available_agents
            if a.status.value != "disabled"
        )

        if not agents_block:
            return ClassificationResult(route_to=[], reason="No active agents")

        payload_str = json.dumps(event.payload, indent=2)[:500]

        prompt = _CLASSIFIER_PROMPT.format(
            agents_block=agents_block,
            topic=event.topic,
            payload=payload_str,
        )

        try:
            response = await self.ollama.chat_sync(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                options={"num_predict": 150, "temperature": 0.1},
            )
            return self._parse_response(response, available_agents)
        except Exception as e:
            logger.error("Classifier error: %s", e)
            return ClassificationResult(route_to=[], reason=f"Classifier error: {e}")

    def _parse_response(self, raw: str, agents: list[Agent]) -> ClassificationResult:
        """Parse classifier response into a routing decision."""
        valid_ids = {a.id for a in agents}

        try:
            # Try direct JSON parse
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            # Try extracting from markdown code block
            try:
                if "```" in raw:
                    block = raw.split("```")[1]
                    if block.startswith("json"):
                        block = block[4:]
                    data = json.loads(block.strip())
                else:
                    # Find first { and last }
                    start = raw.index("{")
                    end = raw.rindex("}") + 1
                    data = json.loads(raw[start:end])
            except (json.JSONDecodeError, ValueError):
                logger.warning("Classifier returned unparseable response: %s", raw[:200])
                return ClassificationResult(route_to=[], reason="Parse failure")

        route_to = data.get("route_to", "no_op")
        reason = data.get("reason", "")

        if route_to == "no_op":
            return ClassificationResult(route_to=[], reason=reason, is_no_op=True)

        # Normalize to list
        if isinstance(route_to, str):
            route_to = [route_to]

        # Validate agent IDs
        valid_routes = [aid for aid in route_to if aid in valid_ids]
        if not valid_routes:
            # Try fuzzy matching (classifier might return name instead of ID)
            name_to_id = {a.name.lower(): a.id for a in agents}
            for aid in route_to:
                matched_id = name_to_id.get(aid.lower())
                if matched_id:
                    valid_routes.append(matched_id)

        if not valid_routes:
            logger.info("Classifier returned unknown agent(s): %s", route_to)
            return ClassificationResult(route_to=[], reason=f"Unknown agent: {route_to}")

        return ClassificationResult(route_to=valid_routes, reason=reason)


class ClassificationResult:
    """Result of event classification."""

    def __init__(
        self,
        route_to: list[str],
        reason: str = "",
        is_no_op: bool = False,
    ):
        self.route_to = route_to
        self.reason = reason
        self.is_no_op = is_no_op

    def __repr__(self) -> str:
        if self.is_no_op:
            return f"ClassificationResult(no_op, reason={self.reason!r})"
        return f"ClassificationResult(route_to={self.route_to}, reason={self.reason!r})"
