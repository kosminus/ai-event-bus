"""FastAPI application — lifespan, routing, static files."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from aiventbus.ai.classifier import EventClassifier
from aiventbus.ai.context_engine import ContextEngine
from aiventbus.ai.ollama_client import OllamaClient
from aiventbus.ai.output_parser import OutputParser
from aiventbus.config import AppConfig, load_config
from aiventbus.consumers.llm_agent import LLMAgentConsumer
from aiventbus.core.assignments import AssignmentManager
from aiventbus.core.bus import EventBus, WebSocketHub
from aiventbus.core.executor import Executor
from aiventbus.core.lifecycle import LifecycleManager
from aiventbus.core.policy import PolicyEngine
from aiventbus.storage.db import Database
from aiventbus.producers.manager import ProducerManager
from aiventbus.storage.repositories import (
    AgentRepository,
    AssignmentRepository,
    EventRepository,
    KnowledgeRepository,
    MemoryRepository,
    PendingActionRepository,
    ProducerRepository,
    ResponseRepository,
    RoutingRuleRepository,
)
from aiventbus.storage.seed_defaults import seed_defaults
from aiventbus.storage.seeder import seed_system_facts

logger = logging.getLogger("aiventbus")


class AgentManager:
    """Manages the lifecycle of LLM agent consumers."""

    def __init__(
        self,
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
        assignment_manager: AssignmentManager,
        policy_engine: PolicyEngine | None = None,
        executor: Executor | None = None,
        action_repo: PendingActionRepository | None = None,
    ):
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
        self.assignment_manager = assignment_manager
        self.policy_engine = policy_engine
        self.executor = executor
        self.action_repo = action_repo
        self._consumers: dict[str, LLMAgentConsumer] = {}

    async def start_agent(self, agent_id: str) -> bool:
        """Start an agent consumer if not already running."""
        if agent_id in self._consumers:
            return True

        agent = await self.agent_repo.get(agent_id)
        if not agent or agent.status.value == "disabled":
            return False

        consumer = LLMAgentConsumer(
            agent=agent,
            bus=self.bus,
            ollama=self.ollama,
            context_engine=self.context_engine,
            output_parser=self.output_parser,
            event_repo=self.event_repo,
            agent_repo=self.agent_repo,
            assignment_repo=self.assignment_repo,
            memory_repo=self.memory_repo,
            response_repo=self.response_repo,
            ws_hub=self.ws_hub,
            policy_engine=self.policy_engine,
            executor=self.executor,
            action_repo=self.action_repo,
        )
        await consumer.start()
        self._consumers[agent_id] = consumer

        # Register notifier so assignments can wake the agent
        self.assignment_manager.register_agent_notifier(agent_id, consumer.notify)
        return True

    async def stop_agent(self, agent_id: str) -> None:
        """Stop an agent consumer."""
        consumer = self._consumers.pop(agent_id, None)
        if consumer:
            await consumer.stop()
            self.assignment_manager.unregister_agent_notifier(agent_id)

    async def start_all(self) -> None:
        """Start consumers for all non-disabled agents."""
        agents = await self.agent_repo.list()
        for agent in agents:
            if agent.status.value != "disabled":
                await self.start_agent(agent.id)
        logger.info("Started %d agent consumers", len(self._consumers))

    async def stop_all(self) -> None:
        """Stop all running consumers."""
        for agent_id in list(self._consumers):
            await self.stop_agent(agent_id)

    def is_running(self, agent_id: str) -> bool:
        return agent_id in self._consumers


# Global app state
_config: AppConfig | None = None
_db: Database | None = None
_bus: EventBus | None = None
_ollama: OllamaClient | None = None
_agent_manager: AgentManager | None = None
_lifecycle: LifecycleManager | None = None
_producer_manager: ProducerManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global _config, _db, _bus, _ollama, _agent_manager, _lifecycle, _producer_manager

    # Load config
    _config = load_config()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, _config.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Initialize database
    _db = Database(_config.database.path)
    await _db.connect()

    # Initialize repositories
    event_repo = EventRepository(_db)
    agent_repo = AgentRepository(_db)
    assignment_repo = AssignmentRepository(_db)
    rule_repo = RoutingRuleRepository(_db)
    memory_repo = MemoryRepository(_db)
    response_repo = ResponseRepository(_db)
    producer_repo = ProducerRepository(_db)

    # Initialize WebSocket hub
    ws_hub = WebSocketHub()

    # Initialize Ollama client
    _ollama = OllamaClient(
        base_url=_config.ollama.base_url,
        timeout=_config.ollama.request_timeout,
    )

    # Initialize core bus
    _bus = EventBus(_config, event_repo, assignment_repo, ws_hub)

    # Initialize knowledge store
    knowledge_repo = KnowledgeRepository(_db)
    await seed_system_facts(knowledge_repo)

    # Seed default agents and routing rules (on first run)
    if _config.seed_defaults:
        await seed_defaults(agent_repo, rule_repo)

    # Initialize AI modules
    context_engine = ContextEngine(event_repo, memory_repo, knowledge_repo=knowledge_repo)
    output_parser = OutputParser()

    # Initialize policy engine and executor
    policy_engine = PolicyEngine(trust_overrides=_config.policy.trust_overrides)
    executor = Executor(shell_timeout=_config.policy.shell_timeout_seconds)
    action_repo = PendingActionRepository(_db)

    # Register knowledge action handlers in executor
    async def _handle_set_knowledge(data: dict) -> dict:
        key = data.get("key", "")
        value = data.get("value", "")
        source = data.get("source", "agent")
        await knowledge_repo.set(key, value, source=source)
        return {"key": key, "status": "stored"}

    async def _handle_get_knowledge(data: dict) -> dict:
        key = data.get("key")
        prefix = data.get("prefix")
        if key:
            entry = await knowledge_repo.get(key)
            if entry:
                return {"key": entry.key, "value": entry.value}
            return {"key": key, "error": "not found"}
        if prefix:
            entries = await knowledge_repo.scan(prefix)
            return {"results": [{"key": e.key, "value": e.value} for e in entries]}
        return {"error": "provide key or prefix"}

    executor.register("set_knowledge", _handle_set_knowledge)
    executor.register("get_knowledge", _handle_get_knowledge)

    # Initialize assignment manager (routing)
    assignment_manager = AssignmentManager(
        _config, event_repo, agent_repo, assignment_repo, rule_repo
    )

    # Wire the router into the bus and give assignment manager a bus reference
    assignment_manager.set_bus(_bus)
    _bus.set_router(assignment_manager.route_event)

    # Initialize classifier for fallback routing
    if _config.classifier.enabled:
        classifier = EventClassifier(
            ollama=_ollama,
            model=_config.classifier.model,
            timeout_seconds=_config.classifier.timeout_seconds,
        )
        assignment_manager.set_classifier(classifier)
        logger.info("Event classifier enabled (model=%s)", _config.classifier.model)

    # Initialize lifecycle manager (expiry + retry)
    _lifecycle = LifecycleManager(_db)
    await _lifecycle.start()

    # Initialize agent manager
    _agent_manager = AgentManager(
        bus=_bus,
        ollama=_ollama,
        context_engine=context_engine,
        output_parser=output_parser,
        event_repo=event_repo,
        agent_repo=agent_repo,
        assignment_repo=assignment_repo,
        memory_repo=memory_repo,
        response_repo=response_repo,
        ws_hub=ws_hub,
        assignment_manager=assignment_manager,
        policy_engine=policy_engine,
        executor=executor,
        action_repo=action_repo,
    )

    # Initialize API modules
    from aiventbus.api import events, agents, routing_rules, ws, system, actions, knowledge, producers

    events.init(_bus, event_repo, assignment_repo, response_repo)
    agents.init(agent_repo, memory_repo)
    routing_rules.init(rule_repo)
    ws.init(ws_hub)
    system.init(_db, _config)
    actions.init(action_repo, executor, _bus, ws_hub)
    knowledge.init(knowledge_repo)

    # Initialize and start producers
    _producer_manager = ProducerManager(bus=_bus, config=_config)
    producers.init(_producer_manager)
    await _producer_manager.start_all()

    # Start all existing agent consumers
    await _agent_manager.start_all()

    # Check Ollama connectivity
    if await _ollama.is_available():
        models = await _ollama.list_models()
        logger.info("Ollama connected. Available models: %s", [m.name for m in models])
    else:
        logger.warning("Ollama not reachable at %s — agents will fail until it's available", _config.ollama.base_url)

    logger.info("AI Event Bus started on http://%s:%d", _config.server.host, _config.server.port)

    yield

    # Shutdown
    await _producer_manager.stop_all()
    await _lifecycle.stop()
    await _agent_manager.stop_all()
    await _ollama.close()
    await _db.close()
    logger.info("AI Event Bus stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AI Event Bus",
        description="A local-first intelligence bus for orchestrating LLM agents",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    from aiventbus.api import events, agents, routing_rules, ws, system, actions, knowledge, producers

    app.include_router(events.router)
    app.include_router(agents.router)
    app.include_router(routing_rules.router)
    app.include_router(ws.router)
    app.include_router(system.router)
    app.include_router(actions.router)
    app.include_router(knowledge.router)
    app.include_router(producers.router)

    # Static files (Web UI)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()


def get_agent_manager() -> AgentManager:
    """Get the agent manager (for use by API endpoints)."""
    return _agent_manager


def cli():
    """CLI entry point."""
    config = load_config()
    uvicorn.run(
        "aiventbus.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=False,
        log_level=config.logging.level.lower(),
    )
