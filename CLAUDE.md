# CLAUDE.md — AI Event Bus

## What is this project?

**aiventbus** is a local-first AI control plane — an event-sourced runtime that sits between the OS (Linux or macOS) and LLM agents, giving your machine ambient intelligence via Ollama.

Events flow from producers → through routing → to LLM agents (consumers) → through policy engine → to executor → whose outputs flow back as new events (chain reactions). All OS-specific plumbing lives behind a single `aiventbus.platform` boundary so the rest of the code thinks in capabilities and topics, not OS names.

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
python -m aiventbus           # foreground — platform-default config + DB
python -m aiventbus --dev     # allow CWD fallbacks (./config.yaml, ./aiventbus.db)
```

Dashboard on `http://localhost:8420`. OpenAPI at `/docs`.

CLI:
```bash
aibus status                 # daemon status
aibus query "question"       # ask a question
aibus events --limit 20      # list events
aibus approve <action_id>    # approve pending action
aibus knowledge list         # list knowledge store
aibus memory list            # list/search long-term memory
aibus memory add ...         # add long-term memory
aibus shell-hook             # print shell preexec hook script
aibus shell-hook --install   # auto-append to ~/.bashrc or ~/.zshrc

aibus install                # systemd user unit (Linux) / LaunchAgent (macOS)
aibus install --build-helper # macOS only: build + install aiventbus-mac-helper
aibus uninstall              # remove unit + helper
aibus uninstall --purge      # also delete config / DB / log dirs
```

Path resolution is deterministic. Order: `--config`/`--db` flag → `$AIVENTBUS_CONFIG` / `$AIVENTBUS_DB` → dev-mode CWD fallback (only under `--dev` / `$AIVENTBUS_DEV=1`) → platform default (`~/.config/aiventbus/` on Linux, `~/Library/Application Support/aiventbus/` on macOS). Never CWD by default, so a systemd/launchd-managed daemon resolves identically to an interactive CLI.

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
├── telemetry.py             # Prometheus metrics + /metrics exposition + HTTP middleware
├── core/
│   ├── bus.py               # EventBus: publish, dedupe, dispatch, WebSocket hub
│   ├── assignments.py       # Pull-based routing + assignment creation + priority lanes
│   ├── lifecycle.py         # Expiry sweeper, retry scheduler
│   ├── policy.py            # Policy engine: blocklist, allowlist, trust modes
│   ├── executor.py          # Action executor: shell, file, notify, http_request, set_knowledge, get_knowledge, tool_call
│   ├── tools.py             # ToolBackend base class + ToolRegistry for pluggable tools
│   └── compression.py       # Backpressure (not yet implemented)
├── ai/
│   ├── context_engine.py    # Builds token-bounded prompts with recalled experience + transcript memory + refs + knowledge
│   ├── output_parser.py     # Parses structured JSON from LLM responses
│   ├── ollama_client.py     # Async streaming Ollama HTTP client
│   ├── classifier.py        # LLM-based routing fallback for unmatched events
│   └── model_selector.py    # Model resolution chain (not yet implemented)
├── consumers/
│   ├── base.py              # Abstract consumer
│   └── llm_agent.py         # Ollama-backed agent worker (claim, process, stream, policy, execute)
├── platform.py              # OS boundary: paths, capability flags, command builders, mac_helper_path
├── install.py               # `aibus install` / `aibus uninstall` — systemd + launchd unit generators
├── shell_hook.sh            # Bash/zsh preexec hook for real-time terminal capture
├── producers/
│   ├── base.py              # Abstract producer
│   ├── manager.py           # Enumerates the registry, capability-gated start/stop
│   ├── registry.py          # ProducerSpec + REGISTRY (name, capabilities, factory, supported_platforms)
│   ├── clipboard.py         # Clipboard monitor (pbpaste on macOS, xclip/wl-paste on Linux)
│   ├── file_watcher.py      # File system watcher (watchfiles → inotify/FSEvents)
│   ├── terminal_monitor.py  # Shell history monitor (bash/zsh)
│   ├── webhook.py           # HTTP webhook receiver (POST → bus events)
│   ├── cron.py              # Scheduled event emitter (cron/interval → bus events)
│   ├── system_log/          # Unified producer: journald on Linux, log stream on macOS
│   │   ├── __init__.py      # SystemLogProducer + shared classifier + backend selector
│   │   └── backends/
│   │       ├── journald.py  # journalctl -f -o json
│   │       └── log_stream.py# log stream --style=ndjson --predicate …
│   └── desktop_events/      # Unified producer: DBus on Linux, Swift helper on macOS
│       ├── __init__.py      # DesktopEventsProducer + backend selector
│       └── backends/
│           ├── dbus.py      # notifications + session lock via dbus_fast
│           └── mac_helper.py# NDJSON stream from aiventbus-mac-helper
├── storage/
│   ├── db.py                # SQLite schema (11 tables + FTS), connection, migrations
│   ├── repositories.py      # CRUD for all entities
│   ├── seeder.py            # System facts auto-seeder (hostname, GPU, memory, etc.)
│   └── seed_defaults.py     # Default agents + routing rules seeder
├── api/
│   ├── events.py            # POST/GET events + trace viewer
│   ├── agents.py            # CRUD agents + enable/disable
│   ├── routing_rules.py     # CRUD routing rules
│   ├── producers.py         # Producers list, enable/disable API
│   ├── webhook.py           # Webhook receiver endpoint (POST /api/v1/webhook/{path})
│   ├── cron.py              # Cron job management API (CRUD scheduled jobs)
│   ├── actions.py           # Confirmation queue (pending/approve/deny)
│   ├── knowledge.py         # Knowledge store CRUD
│   ├── memories.py          # Distilled long-term memory CRUD + search
│   ├── ws.py                # WebSocket hub (multiplexed channels)
│   └── system.py            # Health, topics, status
├── static/
│   ├── index.html           # Dashboard shell
│   ├── style.css            # Dark theme
│   └── app.js               # SPA logic, WebSocket, rendering
widget/
├── src-tauri/               # Rust/Tauri backend (tray, global shortcut, IPC)
│   ├── Cargo.toml
│   ├── tauri.conf.json      # bundle targets: deb + appimage on Linux, dmg + app on macOS
│   └── src/
│       ├── main.rs
│       └── lib.rs
└── src/                     # Widget frontend (vanilla HTML/CSS/JS)
    ├── index.html
    ├── style.css
    └── app.js

bin/
└── aiventbus-mac-helper/    # Swift sidecar — NDJSON stream of macOS desktop events
    ├── Package.swift
    └── Sources/aiventbus-mac-helper/main.swift
```

## Key concepts

- **Event schema is tiered**: core (topic + payload), first-class optional (priority, semantic_type, dedupe_key, trace_id), advanced (token_budget, recommended_model)
- **Pull-based assignments**: routing creates pending assignments, agents claim when idle
- **Priority lanes**: interactive (user.*) > critical (security.*) > ambient (everything else)
- **Structured agent output**: LLMs return `{type, summary, confidence, proposed_actions}` JSON
- **Policy-gated execution**: agents propose actions → policy engine (blocklist → allowlist → trust mode) → executor or confirmation queue
- **Pluggable tool backends**: external tools (Playwright, MCP, custom APIs) register via `ToolBackend` → dispatched through `tool_call` action type
- **Dynamic agent prompts**: available action types generated from executor + tool registry, not hardcoded
- **Chain reactions**: agent `emit_event` actions publish back to the bus with `parent_event` lineage and inherited `trace_id`
- **Knowledge store**: durable key-value facts in SQLite, auto-seeded with system info, injected into prompts
- **Distilled long-term memory**: searchable recalled experience with scope (`agent:<id>`, `user`, `global`) separate from transcript memory and canonical knowledge
- **Classifier fallback**: unmatched events optionally routed by a lightweight LLM classifier
- **System topics**: `system.unmatched`, `system.parse_failure`, `system.agent_failure`, `system.chain_limit`, `system.action_denied`, `system.unknown_action`

## API

All endpoints under `/api/v1/`. OpenAPI docs at `/docs`.

Key endpoints:
- `POST /api/v1/events` — publish event
- `GET /api/v1/events` — list events (filterable by topic, status)
- `GET /api/v1/events/trace/:trace_id` — trace viewer
- `POST /api/v1/agents` — create agent (auto-starts consumer)
- `POST /api/v1/routing-rules` — create routing rule
- `GET /api/v1/actions/pending` — list actions awaiting approval
- `GET /api/v1/actions/history` — all actions (pending + resolved)
- `GET /api/v1/actions/:id` — action detail
- `POST /api/v1/actions/:id/approve` — approve and execute action
- `POST /api/v1/actions/:id/deny` — deny action
- `GET /api/v1/knowledge` — list knowledge (prefix filter)
- `PUT /api/v1/knowledge/:key` — set knowledge entry
- `GET /api/v1/memories` — list/search distilled long-term memories
- `POST /api/v1/memories` — create long-term memory
- `GET /api/v1/memories/:id` — get long-term memory
- `PATCH /api/v1/memories/:id` — update long-term memory importance
- `DELETE /api/v1/memories/:id` — delete long-term memory
- `GET /api/v1/producers` — list all producers with running status
- `POST /api/v1/producers/:name/enable` — start a producer
- `POST /api/v1/producers/:name/disable` — stop a producer
- `POST /api/v1/webhook/{topic_path}` — receive a webhook event
- `GET /api/v1/cron/jobs` — list scheduled cron jobs
- `POST /api/v1/cron/jobs` — add a cron job at runtime
- `DELETE /api/v1/cron/jobs/:name` — remove a cron job
- `GET /api/v1/system/status` — health check
- `GET /api/v1/topics` — topic stats
- `GET /metrics` — Prometheus exposition (plaintext). Mounted at the root, not under `/api/v1/`
- `ws://localhost:8420/ws` — WebSocket (channels: `events:*`, `agents:*`, `system`)

## Producers

Seven built-in producers capture events from OS activity, external systems, and time-based triggers. All are manageable from the web UI Producers tab or via config.

| Producer | Topics | How it works | Default |
|---|---|---|---|
| **Clipboard** | `clipboard.text` | Polls pbpaste (macOS) or xclip / wl-paste (Linux) for new clipboard text | Enabled |
| **File Watcher** | `fs.created`, `fs.modified`, `fs.deleted` | `watchfiles` → inotify / FSEvents. Paths: `~/Downloads`, `~/Documents` by default. Ignores `*.swp`, `*.tmp`, `.git/*`, `__pycache__/*` | Disabled |
| **Terminal Monitor** | `terminal.command` | Polls shell history file for new commands (bash + zsh extended format) | Disabled |
| **System Log** | `syslog.error`, `syslog.warning`, `syslog.auth`, `syslog.service`, `syslog.info` | `journalctl -f -o json` on Linux or `log stream --style=ndjson --predicate …` on macOS. Shared classifier + per-OS noise filter. Classification produces identical topics + payload shape on both OSes | Disabled |
| **Desktop Events** | `session.locked`, `session.unlocked`, `app.launched`, `app.terminated`, `app.activated`, `notification.received` | DBus on Linux (`dbus_fast`, notifications + login1 signals). Swift sidecar (`aiventbus-mac-helper`) on macOS for screen lock/unlock + NSWorkspace app lifecycle. Per-capability availability in `/api/v1/producers` | Disabled |
| **Webhook** | `webhook.{path}` | Receives HTTP POST requests at `/api/v1/webhook/{path}` and publishes them as events. Supports Bearer token and GitHub HMAC auth | Disabled |
| **Cron** | configurable per job | Publishes events on a cron schedule or at fixed intervals using APScheduler. Jobs configurable in config.yaml or via API at runtime | Disabled |

Enable in `config.yaml`:
```yaml
producers:
  clipboard_enabled: true
  file_watcher_enabled: true
  file_watcher_paths: ["~/Downloads", "~/Documents", "~/Projects"]
  dbus_enabled: true                 # drives the unified desktop_events producer
  terminal_monitor_enabled: true
  journald_enabled: true             # drives the unified system_log producer (journald + log_stream)
  journald_priority_filter: 4        # 4=warning+ (default), 3=error+, 7=all; auth/service always pass through
  journald_units: ["sshd", "docker"] # Linux-only: limit journalctl to specific units (empty = all)
  # log_stream_predicate: 'subsystem == "com.apple.xpc.launchd"'  # macOS predicate override
  webhook_enabled: true
  webhook_secret: "my-secret-token"  # optional: Bearer token / HMAC secret
  cron_enabled: true
  cron_timezone: "UTC"
  cron_jobs:
    - name: health_check
      expression: "*/5 * * * *"      # every 5 minutes
      topic: cron.health.check
    - name: daily_summary
      expression: "0 9 * * *"        # every day at 9am
      topic: cron.daily.summary
    - name: cleanup_scan
      expression: "1h"               # shorthand: every hour
      topic: cron.downloads.cleanup
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

On first run (empty database), the bus seeds 8 agents and 9 routing rules so it works out of the box:

| Agent | Handles | Topic pattern |
|---|---|---|
| General Assistant | User queries, test events | `user.*`, `test.*` |
| Clipboard Analyzer | Clipboard content | `clipboard.*` |
| File Watcher Agent | Filesystem changes | `fs.*` |
| Notification Summarizer | Desktop notifications | `notification.*` |
| Terminal Helper | Shell commands | `terminal.*` |
| System Log Analyst | Journal/syslog entries | `syslog.*` |
| Webhook Handler | External webhook events | `webhook.*` |
| Scheduled Task Agent | Cron/scheduled events | `cron.*` |

All agents use the configured Ollama model (`gemma4:latest` by default) and return structured JSON responses.

Disable seeding with `seed_defaults: false` in `config.yaml`. The seeder only runs when the agents table is empty — existing databases are never touched.

## What's implemented

- Core event bus with dedupe, chain depth/budget limits, trace_id
- Routing engine with glob matching + LLM classifier fallback
- Priority lanes (interactive/critical/ambient) with capacity reservation
- LLM agent consumers via Ollama (streaming)
- Policy engine (blocklist, allowlist, trust modes: auto/confirm/deny)
- Executor (shell_exec, file_read, file_write, file_delete, notify, open_app, http_request, tool_call)
- Pluggable tool backend system (ToolBackend + ToolRegistry) for external tools
- Confirmation queue with approve/deny API and web UI (Approvals tab with history)
- Context engine (recalled experience, transcript memory, pinned facts, knowledge store, ref resolution)
- Knowledge store with system facts auto-seeder
- Distilled long-term memory layer with FTS-backed search, scoped recall, API, CLI, and dashboard tab
- Output parser (structured JSON extraction)
- Full web dashboard with real-time WebSocket (events, agents, approvals, memories, producers, config)
- Desktop widget (Tauri — chat, activity feed, approvals, tray icon, Ctrl+Space)
- CLI (`aibus` — query, status, events, approve, deny, knowledge, memory, trace, shell-hook)
- Producers: clipboard monitor, file watcher, DBus listener, terminal monitor, journald, webhook, cron
- Shell preexec hook for real-time terminal command capture (bash + zsh)
- Producers API and web UI tab (list, enable/disable at runtime)
- Default agents and routing rules seeder (8 agents, 9 routes on first run)
- SQLite persistence (11 tables + FTS virtual table) with migrations
- Lifecycle manager (expiry, retry)
- Prometheus telemetry at `GET /metrics` — event/routing/assignment/LLM/executor counters + histograms, queue-depth gauge by lane, LLM token counts, producer emits, `system.*` event counts, classifier fallbacks, HTTP request metrics

## Observability

Prometheus exposition at `GET /metrics` (always on — gate at the network layer). Core module is `aiventbus/telemetry.py`; record helpers (`record_event_published`, `record_assignment_state`, `record_llm_tokens`, `set_queue_depth`, etc.) are called inline from the natural instrumentation points (bus publish, routing, llm_agent loop, executor, classifier, lifecycle retry). Queue depth is sampled in a background task from `assignment_repo.count_pending_by_lane()` — interval set by `telemetry.queue_depth_sample_interval_seconds` (default 5s). The `/metrics` route and HTTP middleware are mounted unconditionally in `create_app()` — do not gate them on `load_config()` at import time, since CLI flags aren't applied until later.

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
