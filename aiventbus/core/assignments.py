"""Pull-based assignment system.

Events are routed to create pending assignments.
Agents claim assignments when ready (pull model).
"""

from __future__ import annotations

import logging
import time
from fnmatch import fnmatch

from aiventbus.config import AppConfig
from aiventbus.models import Event, EventStatus, Lane, Priority
from aiventbus.storage.repositories import (
    AgentRepository,
    AssignmentRepository,
    EventRepository,
    RoutingRuleRepository,
)
from aiventbus.telemetry import (
    ROUTING_DURATION_SECONDS,
    record_assignment_created,
    record_routing_result,
)

# Lazy import to avoid circular deps
_classifier = None

logger = logging.getLogger(__name__)

PRIORITY_LEVELS = {
    Priority.low: 0,
    Priority.medium: 1,
    Priority.high: 2,
    Priority.critical: 3,
}


class RoutingMatch:
    """Result of matching an event to a consumer."""

    def __init__(self, agent_id: str, rule_id: str, model_override: str | None = None, token_budget_override: int | None = None):
        self.agent_id = agent_id
        self.rule_id = rule_id
        self.model_override = model_override
        self.token_budget_override = token_budget_override


class AssignmentManager:
    """Routes events and creates assignments for agents to claim."""

    def __init__(
        self,
        config: AppConfig,
        event_repo: EventRepository,
        agent_repo: AgentRepository,
        assignment_repo: AssignmentRepository,
        rule_repo: RoutingRuleRepository,
    ):
        self.config = config
        self.event_repo = event_repo
        self.agent_repo = agent_repo
        self.assignment_repo = assignment_repo
        self.rule_repo = rule_repo
        self._bus = None  # Set via set_bus() to avoid circular dep
        self._classifier = None  # Set via set_classifier()
        # Callback to notify agents that new work is available
        self._notify_agent: dict[str, callable] = {}

    def set_bus(self, bus) -> None:
        """Set bus reference for system events and status broadcasts."""
        self._bus = bus

    def set_classifier(self, classifier) -> None:
        """Set the event classifier for fallback routing."""
        self._classifier = classifier

    def register_agent_notifier(self, agent_id: str, notifier: callable) -> None:
        """Register a callback to wake an agent when assignments arrive."""
        self._notify_agent[agent_id] = notifier

    def unregister_agent_notifier(self, agent_id: str) -> None:
        self._notify_agent.pop(agent_id, None)

    async def route_event(self, event: Event) -> list[RoutingMatch]:
        """Match event against routing rules, create assignments, notify agents."""
        start = time.monotonic()
        routing_result = "skipped_system"

        # Skip system events
        if event.topic.startswith("system."):
            ROUTING_DURATION_SECONDS.labels(result=routing_result).observe(
                time.monotonic() - start
            )
            return []

        rules = await self.rule_repo.list(enabled_only=True)
        matches: list[RoutingMatch] = []
        seen_agents: set[str] = set()

        for rule in rules:
            if not self._rule_matches(rule, event):
                continue

            # Check agent exists and is not disabled
            agent = await self.agent_repo.get(rule.consumer_id)
            if not agent or agent.status.value == "disabled":
                continue

            # Dedupe: one assignment per event-agent pair
            if rule.consumer_id in seen_agents:
                continue
            seen_agents.add(rule.consumer_id)

            # Fan-out limit
            if len(matches) >= self.config.bus.max_fan_out:
                logger.info(
                    "Fan-out limit reached for event %s (max=%d)",
                    event.id, self.config.bus.max_fan_out,
                )
                break

            matches.append(RoutingMatch(
                agent_id=rule.consumer_id,
                rule_id=rule.id,
                model_override=rule.model_override,
                token_budget_override=rule.token_budget_override,
            ))

        if not matches:
            # No static rules matched — try classifier fallback
            if self._classifier:
                classifier_matches = await self._classify_event(event, seen_agents)
                if classifier_matches:
                    matches = classifier_matches
                    routing_result = "classifier_matched"

            if not matches:
                # Still no match after classifier — emit to system.unmatched
                logger.info("No routing match for event %s (topic=%s)", event.id, event.topic)
                routing_result = "unmatched"
                record_routing_result(routing_result)
                if self._bus:
                    await self._bus.update_event_status(event.id, EventStatus.routed)
                    await self._bus._emit_system_event("system.unmatched", {
                        "event_id": event.id,
                        "topic": event.topic,
                        "semantic_type": event.semantic_type,
                    })
                else:
                    await self.event_repo.update_status(event.id, EventStatus.routed)
                ROUTING_DURATION_SECONDS.labels(result=routing_result).observe(
                    time.monotonic() - start
                )
                return []

        if routing_result != "classifier_matched":
            routing_result = "matched"
        record_routing_result(routing_result)

        # Resolve priority lane for this event
        lane = self._resolve_lane(event)

        # Create assignments and notify agents
        for match in matches:
            # Check if assignment already exists
            if await self.assignment_repo.exists(event.id, match.agent_id):
                continue

            assignment = await self.assignment_repo.create(
                event_id=event.id,
                agent_id=match.agent_id,
                model_used=match.model_override,
                token_budget=match.token_budget_override,
                lane=lane,
            )
            logger.info(
                "Created assignment %s: event %s -> agent %s",
                assignment.id, event.id, match.agent_id,
            )
            record_assignment_created(match.agent_id, lane.value)

            # Notify agent that work is available
            notifier = self._notify_agent.get(match.agent_id)
            if notifier:
                try:
                    await notifier()
                except Exception as e:
                    logger.error("Failed to notify agent %s: %s", match.agent_id, e)

        if self._bus:
            await self._bus.update_event_status(event.id, EventStatus.assigned)
        else:
            await self.event_repo.update_status(event.id, EventStatus.assigned)
        ROUTING_DURATION_SECONDS.labels(result=routing_result).observe(
            time.monotonic() - start
        )
        return matches

    def _rule_matches(self, rule, event: Event) -> bool:
        """Check if a routing rule matches an event. All specified conditions must pass (AND)."""
        if rule.topic_pattern:
            if not fnmatch(event.topic, rule.topic_pattern):
                return False

        if rule.semantic_type_pattern:
            if not event.semantic_type or not fnmatch(event.semantic_type, rule.semantic_type_pattern):
                return False

        if rule.min_priority:
            event_level = PRIORITY_LEVELS.get(event.priority, 0)
            min_level = PRIORITY_LEVELS.get(Priority(rule.min_priority), 0)
            if event_level < min_level:
                return False

        if rule.required_capabilities:
            # The event's required capabilities must be a subset of what the rule expects
            # Actually: the rule's capabilities must be present in the agent's capabilities
            # For now, we skip capability matching at rule level (it's done at agent level)
            pass

        return True

    def _resolve_lane(self, event: Event) -> Lane:
        """Determine the priority lane for an event based on topic and priority."""
        # Explicit critical priority always goes to critical lane
        if event.priority == Priority.critical:
            return Lane.critical

        lanes_cfg = self.config.lanes
        for prefix in lanes_cfg.interactive_prefixes:
            if event.topic.startswith(prefix):
                return Lane.interactive

        for prefix in lanes_cfg.critical_prefixes:
            if event.topic.startswith(prefix):
                return Lane.critical

        return Lane.ambient

    async def _classify_event(self, event: Event, seen_agents: set[str]) -> list[RoutingMatch]:
        """Use the LLM classifier to route an unmatched event."""
        try:
            agents = await self.agent_repo.list()
            active_agents = [a for a in agents if a.status.value != "disabled"]
            if not active_agents:
                return []

            result = await self._classifier.classify(event, active_agents)
            logger.info(
                "Classifier result for event %s: %s",
                event.id, result,
            )

            if result.is_no_op:
                logger.info("Classifier: no_op for event %s (%s)", event.id, result.reason)
                return []

            matches = []
            for agent_id in result.route_to:
                if agent_id in seen_agents:
                    continue
                if len(matches) >= self.config.bus.max_fan_out:
                    break
                matches.append(RoutingMatch(
                    agent_id=agent_id,
                    rule_id="classifier",
                ))
            return matches

        except Exception as e:
            logger.error("Classifier failed for event %s: %s", event.id, e)
            return []
