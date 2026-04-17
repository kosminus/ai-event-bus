"""Default agents and routing rules seeder.

Creates a useful starter set of agents and routes on first run so the bus
works out of the box.  Skips if any agents already exist in the database.
Controlled by ``seed_defaults: true`` in config (default).

Agents whose prompts reference OS-specific tooling (the System Log
Analyst in particular) are rendered from a template using
``aiventbus.platform.platform_facts`` at seed time. That keeps the agent
catalogue stable across OSes — one row per role, same name, same UI
entry — while the prompt itself points at the right commands
(``journalctl``/``systemctl`` on Linux, ``log show``/``launchctl`` on
macOS). If prompt semantics ever genuinely diverge, split with a
deliberate migration rather than pre-emptively.
"""

from __future__ import annotations

import logging
from string import Template

from aiventbus import platform as _platform
from aiventbus.models import AgentCreate, RoutingRuleCreate
from aiventbus.storage.repositories import AgentRepository, RoutingRuleRepository

logger = logging.getLogger(__name__)


_JSON_RESPONSE_SUFFIX = (
    'Always respond with valid JSON: {"type": "analysis"|"action", "summary": "...", '
    '"confidence": 0.0-1.0, "proposed_actions": []}.'
)


# ``string.Template`` uses $placeholders and leaves literal {} alone, which
# matters because the JSON response suffix contains JSON braces that
# str.format() would misinterpret.
_SYSTEM_LOG_ANALYST_TEMPLATE = Template(
    "You are a system log analyst embedded in a local event bus running on "
    "$os_name. You receive log entries classified as errors, warnings, auth "
    "events, or service state changes. For each entry:\n"
    "- Explain what happened in plain language\n"
    "- Assess severity (is this routine or does it need attention?)\n"
    "- For auth events: flag failed logins, suspicious privilege escalation, "
    "brute-force patterns\n"
    "- For service failures: suggest diagnostic steps using $log_tools\n"
    "- For errors: identify the root cause if possible\n"
    "Do NOT be verbose for routine events — just note them briefly. "
    "Be detailed and actionable for genuine problems. "
    + _JSON_RESPONSE_SUFFIX
)


def _render_system_log_analyst_prompt() -> str:
    """Fill the System Log Analyst prompt from live platform facts."""
    facts = _platform.platform_facts()
    return _SYSTEM_LOG_ANALYST_TEMPLATE.substitute(
        os_name=facts.get("os_name") or "this machine",
        log_tools=facts.get("log_tools") or "the system log tools",
    )

# ── Default agents ────────────────────────────────────────────────────
# Each tuple: (AgentCreate kwargs, list of routing-rule defs that target it)
# The model intentionally uses a small, widely-available Ollama model.

_DEFAULT_AGENTS: list[dict] = [
    {
        "name": "General Assistant",
        "model": "gemma4:latest",
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
        "model": "gemma4:latest",
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
        "model": "gemma4:latest",
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
        "model": "gemma4:latest",
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
        "model": "gemma4:latest",
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
        "name": "Webhook Handler",
        "model": "gemma4:latest",
        "system_prompt": (
            "You process webhook events from external systems forwarded by a local event bus. "
            "Webhooks may come from CI/CD pipelines, version control, monitoring, home automation, "
            "or custom scripts. For each event:\n"
            "- Identify the source system and event type\n"
            "- Summarize what happened in plain language\n"
            "- Assess whether this needs user attention or is informational\n"
            "- Suggest follow-up actions when appropriate (e.g. review a PR, check a failed build)\n"
            "Always respond with valid JSON: {\"type\": \"analysis\"|\"action\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Processes incoming webhook events from external systems",
        "capabilities": ["webhook", "integration", "analysis"],
    },
    {
        "name": "Scheduled Task Agent",
        "model": "gemma4:latest",
        "system_prompt": (
            "You handle scheduled (cron) events from a local event bus. "
            "These are periodic triggers — health checks, cleanup scans, summary requests, audits, etc. "
            "For each scheduled event:\n"
            "- Understand what the schedule is requesting\n"
            "- Perform the requested analysis or check using available context and knowledge\n"
            "- Provide actionable output: a summary, a status report, or recommended actions\n"
            "- For recurring checks, compare against previous knowledge when available\n"
            "Always respond with valid JSON: {\"type\": \"analysis\"|\"action\", \"summary\": \"...\", "
            "\"confidence\": 0.0-1.0, \"proposed_actions\": []}."
        ),
        "description": "Handles scheduled/cron events — health checks, summaries, audits",
        "capabilities": ["cron", "scheduling", "analysis"],
    },
    {
        "name": "System Log Analyst",
        "model": "gemma4:latest",
        # Rendered from _SYSTEM_LOG_ANALYST_TEMPLATE at seed time so the
        # prompt points at this OS's log tooling (journalctl/systemctl on
        # Linux, log show/launchctl on macOS). The sentinel is replaced
        # inside seed_defaults() below.
        "system_prompt": "__RENDER_SYSTEM_LOG_ANALYST__",
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
        "name": "Webhooks to Webhook Handler",
        "topic_pattern": "webhook.*",
        "agent_name": "Webhook Handler",
        "priority_order": 55,
    },
    {
        "name": "Cron to Scheduled Task Agent",
        "topic_pattern": "cron.*",
        "agent_name": "Scheduled Task Agent",
        "priority_order": 60,
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

    # Create agents and build name→id map. Templated prompts are
    # rendered here so the live OS facts make it into the DB row.
    name_to_id: dict[str, str] = {}
    for agent_def in _DEFAULT_AGENTS:
        resolved = dict(agent_def)
        if resolved.get("system_prompt") == "__RENDER_SYSTEM_LOG_ANALYST__":
            resolved["system_prompt"] = _render_system_log_analyst_prompt()
        agent = await agent_repo.create(AgentCreate(**resolved))
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
