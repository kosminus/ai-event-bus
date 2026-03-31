# CLAUDE.md — AI Event Bus

## What is this project?

**aiventbus** is a local-first AI control plane — an event-sourced runtime that sits between the Linux OS and LLM agents, giving your machine ambient intelligence via Ollama.

Events flow from producers → through routing → to LLM agents (consumers) → through policy engine → to executor → whose outputs flow back as new events (chain reactions).

## Tech stack

- **Python 3.11+** with **FastAPI** (async)
- **SQLite** via `aiosqlite` (WAL mode) — zero-config persistence
- **Ollama** (`localhost:11434`) — local LLM inference
- **Vanilla JS** frontend — no build step, served as static files
- **Tauri 2** (Rust) — desktop widget app
- **httpx** for async streaming to Ollama
- **click** for CLI (`aibus` command)

## Running

```bash
pip install -e .
python -m aiventbus          # http://localhost:8420
```

CLI:
```bash
aibus status                 # daemon status
aibus query "question"       # ask a question
aibus events --limit 20      # list events
aibus approve <action_id>    # approve pending action
aibus knowledge list         # list knowledge store
```

Desktop widget:
```bash
cd widget && cargo tauri dev   # development mode
cd widget && cargo tauri build # production build
```

Config is optional via `config.yaml` in project root. Sensible defaults if absent.

## Project structure

```
aiventbus/
├── main.py                  # FastAPI app, lifespan, AgentManager
├── config.py                # YAML config loader
├── models.py                # Pydantic models (tiered event schema)
├── cli.py                   # CLI interface (aibus command)
├── core/
│   ├── bus.py               # EventBus: publish, dedupe, dispatch, WebSocket hub
│   ├── assignments.py       # Pull-based routing + assignment creation + priority lanes
│   ├── lifecycle.py         # Expiry sweeper, retry scheduler
│   ├── policy.py            # Policy engine: blocklist, allowlist, trust modes
│   ├── executor.py          # Action executor: shell, file, notify, knowledge
│   └── compression.py       # Backpressure (not yet implemented)
├── ai/
│   ├── context_engine.py    # Builds token-bounded prompts with memory + refs + knowledge
│   ├── output_parser.py     # Parses structured JSON from LLM responses
│   ├── ollama_client.py     # Async streaming Ollama HTTP client
│   ├── classifier.py        # LLM-based routing fallback for unmatched events
│   └── model_selector.py    # Model resolution chain (not yet implemented)
├── consumers/
│   ├── base.py              # Abstract consumer
│   └── llm_agent.py         # Ollama-backed agent worker (claim, process, stream, policy, execute)
├── producers/
│   ├── base.py              # Abstract producer
│   ├── manager.py           # ProducerManager (lifecycle for all producers)
│   ├── clipboard.py         # Clipboard monitor (X11/Wayland)
│   ├── file_watcher.py      # File system watcher (watchfiles/inotify)
│   ├── dbus_listener.py     # DBus session bus listener (notifications, session lock)
│   └── terminal_monitor.py  # Shell history monitor (bash/zsh)
├── storage/
│   ├── db.py                # SQLite schema (10 tables), connection, migrations
│   ├── repositories.py      # CRUD for all entities
│   └── seeder.py            # System facts auto-seeder (hostname, GPU, memory, etc.)
├── api/
│   ├── events.py            # POST/GET events + trace viewer
│   ├── agents.py            # CRUD agents + enable/disable
│   ├── routing_rules.py     # CRUD routing rules
│   ├── actions.py           # Confirmation queue (pending/approve/deny)
│   ├── knowledge.py         # Knowledge store CRUD
│   ├── ws.py                # WebSocket hub (multiplexed channels)
│   └── system.py            # Health, topics, status
├── static/
│   ├── index.html           # Dashboard shell
│   ├── style.css            # Dark theme
│   └── app.js               # SPA logic, WebSocket, rendering
widget/
├── src-tauri/               # Rust/Tauri backend (tray, global shortcut, IPC)
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   └── src/
│       ├── main.rs
│       └── lib.rs
└── src/                     # Widget frontend (vanilla HTML/CSS/JS)
    ├── index.html
    ├── style.css
    └── app.js
```

## Key concepts

- **Event schema is tiered**: core (topic + payload), first-class optional (priority, semantic_type, dedupe_key, trace_id), advanced (token_budget, recommended_model)
- **Pull-based assignments**: routing creates pending assignments, agents claim when idle
- **Priority lanes**: interactive (user.*) > critical (security.*) > ambient (everything else)
- **Structured agent output**: LLMs return `{type, summary, confidence, proposed_actions}` JSON
- **Policy-gated execution**: agents propose actions → policy engine (blocklist → allowlist → trust mode) → executor or confirmation queue
- **Chain reactions**: agent `emit_event` actions publish back to the bus with `parent_event` lineage and inherited `trace_id`
- **Knowledge store**: durable key-value facts in SQLite, auto-seeded with system info, injected into prompts
- **Classifier fallback**: unmatched events optionally routed by a lightweight LLM classifier
- **System topics**: `system.unmatched`, `system.parse_failure`, `system.agent_failure`, `system.chain_limit`, `system.action_denied`

## API

All endpoints under `/api/v1/`. OpenAPI docs at `/docs`.

Key endpoints:
- `POST /api/v1/events` — publish event
- `GET /api/v1/events` — list events (filterable by topic, status)
- `GET /api/v1/events/trace/:trace_id` — trace viewer
- `POST /api/v1/agents` — create agent (auto-starts consumer)
- `POST /api/v1/routing-rules` — create routing rule
- `GET /api/v1/actions/pending` — list actions awaiting approval
- `POST /api/v1/actions/:id/approve` — approve and execute action
- `POST /api/v1/actions/:id/deny` — deny action
- `GET /api/v1/knowledge` — list knowledge (prefix filter)
- `PUT /api/v1/knowledge/:key` — set knowledge entry
- `GET /api/v1/system/status` — health check
- `GET /api/v1/topics` — topic stats
- `ws://localhost:8420/ws` — WebSocket (channels: `events:*`, `agents:*`, `system`)

## What's implemented

- Core event bus with dedupe, chain depth/budget limits, trace_id
- Routing engine with glob matching + LLM classifier fallback
- Priority lanes (interactive/critical/ambient) with capacity reservation
- LLM agent consumers via Ollama (streaming)
- Policy engine (blocklist, allowlist, trust modes: auto/confirm/deny)
- Executor (shell_exec, file_read, file_write, file_delete, notify, open_app)
- Confirmation queue with approve/deny API
- Context engine (memory, pinned facts, knowledge store, ref resolution)
- Knowledge store with system facts auto-seeder
- Output parser (structured JSON extraction)
- Full web dashboard with real-time WebSocket
- Desktop widget (Tauri — chat, activity feed, approvals, tray icon, Ctrl+Space)
- CLI (`aibus` — query, status, events, approve, deny, knowledge, trace)
- Producers: clipboard monitor, file watcher, DBus listener, terminal monitor
- SQLite persistence (10 tables) with migrations
- Lifecycle manager (expiry, retry)

## Development notes

- Database file: `./aiventbus.db` (auto-created on first run)
- No build step for web frontend — edit static files directly
- Widget: `cd widget && cargo tauri dev` (requires Rust toolchain)
- Ollama must be running at `localhost:11434` for agents to work
- Agents auto-start on creation and on server startup
- The bus emits system events for debugging — check `system.*` topics
- Producers disabled by default (except clipboard) — enable in config.yaml
- Classifier disabled by default — enable with `classifier.enabled: true`
- Policy trust modes configurable via `policy.trust_overrides` in config.yaml
