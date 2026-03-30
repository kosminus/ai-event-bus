# CLAUDE.md — AI Event Bus

## What is this project?

**aiventbus** is a local-first intelligence bus — an event-driven runtime for orchestrating multiple LLM agents via Ollama. Think "Kafka meets AI agent framework" but running entirely on your machine.

Events flow from producers → through routing → to LLM agents (consumers) → whose structured outputs flow back as new events (chain reactions).

## Tech stack

- **Python 3.11+** with **FastAPI** (async)
- **SQLite** via `aiosqlite` (WAL mode) — zero-config persistence
- **Ollama** (`localhost:11434`) — local LLM inference
- **Vanilla JS** frontend — no build step, served as static files
- **httpx** for async streaming to Ollama

## Running

```bash
pip install -e .
python -m aiventbus          # http://localhost:8420
```

Config is optional via `config.yaml` in project root. Sensible defaults if absent.

## Project structure

```
aiventbus/
├── main.py                  # FastAPI app, lifespan, AgentManager
├── config.py                # YAML config loader
├── models.py                # Pydantic models (tiered event schema)
├── core/
│   ├── bus.py               # EventBus: publish, dedupe, dispatch, WebSocket hub
│   ├── assignments.py       # Pull-based routing + assignment creation
│   ├── lifecycle.py         # Expiry sweeper, retry scheduler
│   └── compression.py       # Backpressure (not yet implemented)
├── ai/
│   ├── context_engine.py    # Builds token-bounded prompts with memory + refs
│   ├── output_parser.py     # Parses structured JSON from LLM responses
│   ├── ollama_client.py     # Async streaming Ollama HTTP client
│   ├── classifier.py        # Optional enrichment stage (not yet implemented)
│   └── model_selector.py    # Model resolution chain (not yet implemented)
├── consumers/
│   ├── base.py              # Abstract consumer
│   └── llm_agent.py         # Ollama-backed agent worker (claim, process, stream)
├── producers/
│   ├── base.py              # Abstract producer (not yet implemented)
│   └── ...                  # cron, file_watcher, webhook, etc. (not yet implemented)
├── storage/
│   ├── db.py                # SQLite schema (8 tables), connection
│   └── repositories.py      # CRUD for all entities
├── api/
│   ├── events.py            # POST/GET events
│   ├── agents.py            # CRUD agents + enable/disable
│   ├── routing_rules.py     # CRUD routing rules
│   ├── ws.py                # WebSocket hub (multiplexed channels)
│   └── system.py            # Health, topics, status
└── static/
    ├── index.html           # Dashboard shell
    ├── style.css            # Dark theme
    └── app.js               # SPA logic, WebSocket, rendering
```

## Key concepts

- **Event schema is tiered**: core (topic + payload), first-class optional (priority, semantic_type, dedupe_key), advanced (token_budget, recommended_model — not yet used)
- **Pull-based assignments**: routing creates pending assignments, agents claim when idle
- **Structured agent output**: LLMs return `{type, summary, confidence, proposed_actions}` JSON. Parse failures go to `system.parse_failure`
- **Chain reactions**: agent `emit_event` actions publish back to the bus with `parent_event` lineage
- **System topics**: `system.unmatched`, `system.parse_failure`, `system.agent_failure`, `system.chain_limit`

## API

All endpoints under `/api/v1/`. OpenAPI docs at `/docs`.

Key endpoints:
- `POST /api/v1/events` — publish event
- `GET /api/v1/events` — list events (filterable by topic, status)
- `POST /api/v1/agents` — create agent (auto-starts consumer)
- `POST /api/v1/routing-rules` — create routing rule
- `GET /api/v1/system/status` — health check
- `GET /api/v1/topics` — topic stats
- `ws://localhost:8420/ws` — WebSocket (channels: `events:*`, `agents:*`, `system`)

## What's implemented vs planned

**Working:**
- Core event bus with dedupe, chain depth/budget limits
- Routing engine with glob matching
- LLM agent consumers via Ollama (streaming)
- Context engine (memory, pinned facts, ref resolution)
- Output parser (structured JSON extraction)
- Full web dashboard with real-time WebSocket
- SQLite persistence (8 tables)
- Lifecycle manager (expiry, retry)

**Not yet implemented:**
- Producers (cron, file_watcher, log_tail, webhook, fixture, replay)
- Backpressure compression
- Classifier/enrichment stage
- Model selector (fallback chain)
- Observer/audit agent type

## Development notes

- Database file: `./aiventbus.db` (auto-created on first run)
- No build step for frontend — edit static files directly
- Ollama must be running at `localhost:11434` for agents to work
- Agents auto-start on creation and on server startup
- The bus emits system events for debugging — check `system.*` topics
