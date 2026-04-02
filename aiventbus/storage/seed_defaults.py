"""Default agents and routing rules seeder.

Creates a useful starter set of agents and routes on first run so the bus
works out of the box.  Skips if any agents already exist in the database.
Controlled by ``seed_defaults: true`` in config (default).
"""

from __future__ import annotations

import logging

from aiventbus.models import AgentCreate, RoutingRuleCreate
from aiventbus.storage.repositories import AgentRepository, RoutingRuleRepository

logger = logging.getLogger(__name__)

# ── Default agents ────────────────────────────────────────────────────
# Each tuple: (AgentCreate kwargs, list of routing-rule defs that target it)
# The model intentionally uses a small, widely-available Ollama model.

_DEFAULT_AGENTS: list[dict] = [
    {
        "name": "General Assistant",
        "model": "llama3.1:8b",
        "system_prompt": (
            "You are a general-purpose AI assistant embedded in a local event bus. "
            "You receive events from the user and from system producers. "
            "Analyze the event, provide a concise summary, and suggest actions when appropriate. "
            "Always respond with valid JSON: {\"type\": \"analysis\"|\"action\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Catch-all agent for user queries and unmatched events",
        "capabilities": ["general", "analysis"],
    },
    {
        "name": "Clipboard Analyzer",
        "model": "llama3.1:8b",
        "system_prompt": (
            "You analyze clipboard contents forwarded by a local event bus. "
            "Determine what the user copied — code snippet, URL, error message, prose, etc. "
            "Provide a brief summary of the content and suggest useful follow-up actions "
            "(e.g. explain code, open URL, search for error). "
            "Always respond with valid JSON: {\"type\": \"analysis\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Analyzes clipboard text — identifies content type and suggests actions",
        "capabilities": ["clipboard", "analysis"],
    },
    {
        "name": "File Watcher Agent",
        "model": "llama3.1:8b",
        "system_prompt": (
            "You monitor file system changes reported by a local event bus. "
            "When a file is created, modified, or deleted, summarize the change and assess "
            "whether it looks routine or noteworthy (e.g. large download, config change, "
            "suspicious binary). Suggest actions only when warranted. "
            "Always respond with valid JSON: {\"type\": \"analysis\"|\"action\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Monitors filesystem events and flags noteworthy changes",
        "capabilities": ["filesystem", "analysis"],
    },
    {
        "name": "Notification Summarizer",
        "model": "llama3.1:8b",
        "system_prompt": (
            "You summarize desktop notifications forwarded by a local event bus. "
            "Group related notifications, filter noise, and surface anything the user "
            "should act on. "
            "Always respond with valid JSON: {\"type\": \"analysis\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Summarizes desktop notifications and highlights important ones",
        "capabilities": ["notifications", "analysis"],
    },
    {
        "name": "Terminal Helper",
        "model": "llama3.1:8b",
        "system_prompt": (
            "You observe shell commands executed by the user, forwarded via a local event bus. "
            "When you see a command, briefly note what it does. If it looks like the user hit "
            "an error or is trying something complex, offer a helpful tip or alternative. "
            "Do NOT be intrusive for routine commands — just acknowledge them. "
            "Always respond with valid JSON: {\"type\": \"analysis\"|\"action\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Watches shell history and offers tips on errors or complex commands",
        "capabilities": ["terminal", "analysis"],
    },
    {
        "name": "System Log Analyst",
        "model": "llama3.1:8b",
        "system_prompt": (
            "You are a Linux system log analyst embedded in a local event bus. "
            "You receive journal/syslog entries classified as errors, warnings, auth events, "
            "or service state changes. For each entry:\n"
            "- Explain what happened in plain language\n"
            "- Assess severity (is this routine or does it need attention?)\n"
            "- For auth events: flag failed logins, suspicious sudo usage, brute-force patterns\n"
            "- For service failures: suggest diagnostic steps (journalctl -u, systemctl status)\n"
            "- For errors: identify the root cause if possible\n"
            "Do NOT be verbose for routine events — just note them briefly. "
            "Be detailed and actionable for genuine problems. "
            "Always respond with valid JSON: {\"type\": \"analysis\"|\"action\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Analyzes system journal entries — flags errors, auth issues, service failures",
        "capabilities": ["syslog", "security", "analysis"],
    },
]

# ── Default routing rules ─────────────────────────────────────────────
# Maps topic patterns to agent names (resolved to IDs at seed time).

_DEFAULT_RULES: list[dict] = [
    {
        "name": "Clipboard to Clipboard Analyzer",
        "topic_pattern": "clipboard.*",
        "agent_name": "Clipboard Analyzer",
        "priority_order": 10,
    },
    {
        "name": "Filesystem to File Watcher Agent",
        "topic_pattern": "fs.*",
        "agent_name": "File Watcher Agent",
        "priority_order": 20,
    },
    {
        "name": "Notifications to Summarizer",
        "topic_pattern": "notification.*",
        "agent_name": "Notification Summarizer",
        "priority_order": 30,
    },
    {
        "name": "Terminal to Helper",
        "topic_pattern": "terminal.*",
        "agent_name": "Terminal Helper",
        "priority_order": 40,
    },
    {
        "name": "User queries to General Assistant",
        "topic_pattern": "user.*",
        "agent_name": "General Assistant",
        "priority_order": 50,
    },
    {
        "name": "Syslog to System Log Analyst",
        "topic_pattern": "syslog.*",
        "agent_name": "System Log Analyst",
        "priority_order": 45,
    },
    {
        "name": "Catch-all to General Assistant",
        "topic_pattern": "test.*",
        "agent_name": "General Assistant",
        "priority_order": 100,
    },
]


async def seed_defaults(
    agent_repo: AgentRepository,
    rule_repo: RoutingRuleRepository,
) -> None:
    """Create default agents and routing rules if the database is empty."""
    existing_agents = await agent_repo.list()
    if existing_agents:
        return  # already has agents — don't touch

    logger.info("Seeding default agents and routing rules ...")

    # Create agents and build name→id map
    name_to_id: dict[str, str] = {}
    for agent_def in _DEFAULT_AGENTS:
        agent = await agent_repo.create(AgentCreate(**agent_def))
        name_to_id[agent_def["name"]] = agent.id
        logger.info("  Created agent: %s (%s)", agent.name, agent.id)

    # Create routing rules
    for rule_def in _DEFAULT_RULES:
        agent_id = name_to_id.get(rule_def["agent_name"])
        if not agent_id:
            continue
        await rule_repo.create(
            RoutingRuleCreate(
                name=rule_def["name"],
                topic_pattern=rule_def["topic_pattern"],
                consumer_id=agent_id,
                priority_order=rule_def["priority_order"],
            )
        )
        logger.info("  Created rule: %s → %s", rule_def["name"], agent_id)

    logger.info(
        "Seeded %d agents and %d routing rules",
        len(_DEFAULT_AGENTS),
        len(_DEFAULT_RULES),
    )
