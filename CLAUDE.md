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
aibus shell-hook             # print shell preexec hook script
aibus shell-hook --install   # auto-append to ~/.bashrc or ~/.zshrc
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
├── shell_hook.sh            # Bash/zsh preexec hook for real-time terminal capture
├── producers/
│   ├── base.py              # Abstract producer
│   ├── manager.py           # ProducerManager (lifecycle, enable/disable at runtime)
│   ├── clipboard.py         # Clipboard monitor (X11/Wayland)
│   ├── file_watcher.py      # File system watcher (watchfiles/inotify)
│   ├── dbus_listener.py     # DBus session bus listener (notifications, session lock)
│   ├── terminal_monitor.py  # Shell history monitor (bash/zsh)
│   └── journald.py          # Systemd journal stream (errors, auth, services)
├── storage/
│   ├── db.py                # SQLite schema (10 tables), connection, migrations
│   ├── repositories.py      # CRUD for all entities
│   ├── seeder.py            # System facts auto-seeder (hostname, GPU, memory, etc.)
│   └── seed_defaults.py     # Default agents + routing rules seeder
├── api/
│   ├── events.py            # POST/GET events + trace viewer
│   ├── agents.py            # CRUD agents + enable/disable
│   ├── routing_rules.py     # CRUD routing rules
│   ├── producers.py         # Producers list, enable/disable API
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
- `GET /api/v1/producers` — list all producers with running status
- `POST /api/v1/producers/:name/enable` — start a producer
- `POST /api/v1/producers/:name/disable` — stop a producer
- `GET /api/v1/system/status` — health check
- `GET /api/v1/topics` — topic stats
- `ws://localhost:8420/ws` — WebSocket (channels: `events:*`, `agents:*`, `system`)

## Producers

Five built-in producers capture OS-level events. All are manageable from the web UI Producers tab or via config.

| Producer | Topics | How it works | Default |
|---|---|---|---|
| **Clipboard** | `clipboard.text` | Polls xclip/wl-paste for new clipboard text | Enabled |
| **File Watcher** | `fs.created`, `fs.modified`, `fs.deleted` | Uses `watchfiles` (inotify) to watch directories recursively. Paths: `~/Downloads`, `~/Documents` by default. Ignores `*.swp`, `*.tmp`, `.git/*`, `__pycache__/*` | Disabled |
| **DBus Listener** | `notification.received`, `session.locked`, `session.unlocked` | Subscribes to freedesktop DBus signals. Requires `dbus-fast` package | Disabled |
| **Terminal Monitor** | `terminal.command` | Polls shell history file for new commands (supports bash and zsh extended format) | Disabled |
| **Journald** | `syslog.error`, `syslog.warning`, `syslog.auth`, `syslog.service`, `syslog.info` | Streams `journalctl -f -o json`. Classifies by priority & facility. Filters noise by default | Disabled |

Enable in `config.yaml`:
```yaml
producers:
  clipboard_enabled: true
  file_watcher_enabled: true
  file_watcher_paths: ["~/Downloads", "~/Documents", "~/Projects"]
  dbus_enabled: true
  terminal_monitor_enabled: true
  journald_enabled: true
  journald_priority_filter: 4        # 4=warning+ (default), 3=error+, 7=all; auth/service always pass through
  journald_units: ["sshd", "docker"] # limit to specific units (empty = all)
```

Or toggle at runtime from the **Producers** tab in the web dashboard.

### Shell preexec hook (real-time terminal capture)

The history-polling terminal monitor only sees commands after the shell flushes its history file. For **real-time** capture, use the preexec hook instead:

```bash
eval "$(aibus shell-hook)"             # activate in current shell
aibus shell-hook --install             # append to ~/.bashrc or ~/.zshrc permanently
```

The hook fires a `terminal.command` event **before** each command runs. Payload includes `command`, `shell`, and `cwd`. Works with both zsh (native `preexec`) and bash (DEBUG trap). Runs `curl` in the background — zero impact on shell performance.

Set `AIVENTBUS_URL` to override the default `http://localhost:8420`.

## Default agents and routing rules

On first run (empty database), the bus seeds 6 agents and 7 routing rules so it works out of the box:

| Agent | Handles | Topic pattern |
|---|---|---|
| General Assistant | User queries, test events | `user.*`, `test.*` |
| Clipboard Analyzer | Clipboard content | `clipboard.*` |
| File Watcher Agent | Filesystem changes | `fs.*` |
| Notification Summarizer | Desktop notifications | `notification.*` |
| Terminal Helper | Shell commands | `terminal.*` |
| System Log Analyst | Journal/syslog entries | `syslog.*` |

All agents use the configured Ollama model (`llama3.1:8b` by default) and return structured JSON responses.

Disable seeding with `seed_defaults: false` in `config.yaml`. The seeder only runs when the agents table is empty — existing databases are never touched.

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
- CLI (`aibus` — query, status, events, approve, deny, knowledge, trace, shell-hook)
- Producers: clipboard monitor, file watcher, DBus listener, terminal monitor, journald
- Shell preexec hook for real-time terminal command capture (bash + zsh)
- Producers API and web UI tab (list, enable/disable at runtime)
- Default agents and routing rules seeder (6 agents, 7 routes on first run)
- SQLite persistence (10 tables) with migrations
- Lifecycle manager (expiry, retry)

## Development notes

- Database file: `./aiventbus.db` (auto-created on first run)
- No build step for web frontend — edit static files directly
- Widget: `cd widget && cargo tauri dev` (requires Rust toolchain)
- Ollama must be running at `localhost:11434` for agents to work
- Agents auto-start on creation and on server startup
- The bus emits system events for debugging — check `system.*` topics
- Producers disabled by default (except clipboard) — enable in config.yaml or via web UI
- Shell hook: `eval "$(aibus shell-hook)"` for real-time terminal capture
- Default agents seed on first run — disable with `seed_defaults: false` in config.yaml
- Classifier disabled by default — enable with `classifier.enabled: true`
- Policy trust modes configurable via `policy.trust_overrides` in config.yaml
