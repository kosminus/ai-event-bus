# Developer Guide

How aiventbus works internally, how to extend it, and where to find things.

## Architecture overview

```
Producers → EventBus → AssignmentManager → LLMAgentConsumer → PolicyEngine → Executor
                ↑                                                                  │
                └────────────────── chain reactions (emit_event) ──────────────────┘
```

The daemon is a single-process async Python app. Everything runs in one asyncio event loop. SQLite (WAL mode) handles persistence. No external services except Ollama.

## Startup flow

`main.py:lifespan()` initializes everything in order:

1. Load config from `config.yaml`
2. Open SQLite database, run schema + migrations
3. Create all repositories (EventRepo, AgentRepo, etc.)
4. Create WebSocketHub
5. Create OllamaClient
6. Create KnowledgeRepository, seed system facts
7. Create ContextEngine, OutputParser
8. Create PolicyEngine, Executor, register action handlers
9. Create EventBus, AssignmentManager, wire them together
10. Create EventClassifier (if enabled), attach to AssignmentManager
11. Create LifecycleManager (expiry + retry loops)
12. Create AgentManager, start all non-disabled agents
13. Create ProducerManager, start configured producers
14. Initialize all API routers

On shutdown: stop producers → stop lifecycle → stop agents → close Ollama → close DB.

## Data flow: event lifecycle

```
EventCreate (from API, producer, or chain reaction)
    │
    ▼
EventBus.publish()
    ├─ Dedupe check (if dedupe_key set, within time window)
    ├─ Chain depth check (max_chain_depth, walks parent chain)
    ├─ Chain budget check (max_chain_budget, counts descendants)
    ├─ Assign trace_id (generate for root, inherit from parent)
    ├─ Persist to SQLite
    ├─ Broadcast event.new via WebSocket
    └─ Route async (non-blocking)
         │
         ▼
    AssignmentManager.route_event()
         ├─ Skip system.* events
         ├─ Match against enabled RoutingRules (fnmatch on topic + semantic_type)
         ├─ If no match + classifier enabled → EventClassifier.classify()
         ├─ If still no match → emit system.unmatched
         ├─ Resolve priority lane (interactive/critical/ambient)
         ├─ Create EventAssignment(s)
         └─ Notify agent(s) via registered callbacks
              │
              ▼
         LLMAgentConsumer._run_loop()
              ├─ Acquire semaphore (respects max_concurrent)
              ├─ Capacity reservation (last slot reserved for interactive lane)
              ├─ claim_next() — atomic UPDATE with priority ordering
              │
              ▼
         _process_assignment()
              ├─ Build prompt via ContextEngine:
              │    system prompt → pinned facts → knowledge → memory → context refs → event
              ├─ Stream Ollama response (tokens broadcast via WebSocket)
              ├─ Parse structured output (OutputParser)
              ├─ Store memory (user + assistant messages)
              ├─ Store AgentResponse
              └─ Execute proposed_actions:
                   │
                   ▼
              _execute_action()
                   ├─ emit_event/log/alert → handled directly (always auto)
                   └─ Everything else → PolicyEngine.evaluate()
                        ├─ deny → log + emit system.action_denied
                        ├─ auto → Executor.execute() immediately
                        └─ confirm → create PendingAction, notify via WebSocket
                             │
                             ▼ (user approves via API/CLI/widget)
                        Executor.execute() → result stored
```

## Database schema

10 tables in `storage/db.py`:

| Table | Purpose |
|-------|---------|
| `events` | All events with topic, payload, status, trace_id, parent chain |
| `agents` | LLM agent definitions (model, prompt, capabilities) |
| `producers` | Producer registrations |
| `routing_rules` | Topic/semantic_type pattern matching rules |
| `event_assignments` | Event-to-agent work tracking with priority lane |
| `agent_memory` | Per-agent conversation history (scoped) |
| `agent_pinned_facts` | Persistent facts per agent |
| `agent_responses` | Stored LLM responses with parsed output |
| `knowledge` | Global key-value fact store |
| `pending_actions` | Actions awaiting user confirmation |

Migrations run in `Database._run_migrations()` for schema changes on existing DBs.

## Key modules

### EventBus (`core/bus.py`)

Central nervous system. `publish()` is the main entry point. Also manages `WebSocketHub` for real-time broadcasting.

Key methods:
- `publish(event_create, producer_id)` → full pipeline
- `update_event_status(event_id, status)` → persist + broadcast
- `_emit_system_event(topic, payload)` → system events (not routed, prevents loops)

### AssignmentManager (`core/assignments.py`)

Routes events to agents. Two-stage: static rules first, classifier fallback.

Key methods:
- `route_event(event)` → match rules, create assignments, notify agents
- `_resolve_lane(event)` → determine priority lane from topic/priority
- `_classify_event(event, seen_agents)` → LLM classifier fallback

### PolicyEngine (`core/policy.py`)

Three-layer safety gate:

1. **Blocklist** — compiled regex for dangerous patterns (`rm -rf /`, `sudo`, fork bombs). Always deny.
2. **Allowlist** — safe read-only commands (`ls`, `df`, `git status`). Auto-approve for `shell_exec`.
3. **Trust mode table** — default trust per action type. Overridable via config.

### Executor (`core/executor.py`)

Action type registry. Each action type has a handler function. Built-in handlers:
- `shell_exec` → `asyncio.create_subprocess_shell` with timeout
- `file_read` / `file_write` / `file_delete` → pathlib operations
- `notify` → `notify-send` subprocess
- `open_app` → `xdg-open`
- `http_request` → `httpx.AsyncClient` with configurable timeout and response size cap
- `set_knowledge` / `get_knowledge` → knowledge store operations
- `tool_call` → dispatches to registered `ToolBackend` plugins (e.g. Playwright)

Register custom handlers: `executor.register("my_action", handler_fn)`

### ContextEngine (`ai/context_engine.py`)

Assembles token-bounded prompts. Priority allocation:
1. System prompt (always)
2. Pinned facts (max 30% budget)
3. Knowledge store entries (max 20% budget, topic-based + system.* + user.*)
4. Memory/conversation history (max 30% budget)
5. Context refs (max 80% budget)
6. Current event (always)

### EventClassifier (`ai/classifier.py`)

Uses a lightweight model to classify unmatched events. Returns `route_to` (agent ID list) or `no_op`. Validates agent IDs against available agents with fuzzy name matching fallback.

### LLMAgentConsumer (`consumers/llm_agent.py`)

The agent worker. Runs as an asyncio task per agent. Pull-based: claims assignments via atomic SQL UPDATE. Concurrency controlled by semaphore (agent.max_concurrent).

### Telemetry (`telemetry.py`)

Owns every Prometheus metric and the `/metrics` exposition endpoint. No wrappers or decorators — business code imports record helpers and calls them inline at instrumentation points.

Metric families and where they're produced:

| Metric | Produced in |
|---|---|
| `aiventbus_http_requests_total`, `..._duration_seconds` | `http_metrics_middleware` (registered on the ASGI app) |
| `aiventbus_events_published_total`, `..._deduped_total`, `..._chain_limit_total`, `event_publish_duration_seconds` | `core/bus.py` — inside `publish()` |
| `aiventbus_system_events_total` | `core/bus.py` — inside `_emit_system_event` |
| `aiventbus_routing_decisions_total`, `..._duration_seconds`, `assignments_created_total` | `core/assignments.py` |
| `aiventbus_classifier_fallbacks_total` | `ai/classifier.py` — `classify()` return sites |
| `aiventbus_assignment_state_transitions_total` | `consumers/llm_agent.py` (claim/complete/fail) and `core/lifecycle.py` (retry) |
| `aiventbus_assignment_queue_depth` | Background sampler in `main.lifespan` — calls `assignment_repo.count_pending_by_lane()` every `telemetry.queue_depth_sample_interval_seconds` and calls `set_queue_depth(lane, n)` |
| `aiventbus_agent_runs_total`, `..._duration_seconds`, `llm_requests_total`, `..._duration_seconds`, `llm_parse_failures_total` | `consumers/llm_agent.py` |
| `aiventbus_llm_tokens_total` | `consumers/llm_agent.py._stream_ollama` — pulls `prompt_eval_count` / `eval_count` via `OllamaClient.chat(stats_out=...)` |
| `aiventbus_producer_events_emitted_total` | `core/bus.py` — when `publish()` is called with a `producer_id` |
| `aiventbus_action_executions_total`, `..._duration_seconds` | `core/executor.py` |

The module has a fallback shim so it imports even when `prometheus_client` isn't installed (label cardinality and render format are preserved for tests).

Keep labels low-cardinality: topics are bucketed (`topic_prefix(...)`) where possible; agent IDs and action types are bounded sets; don't pass event IDs, user inputs, or unbounded strings as label values. When you add a new metric:

1. Register the `Counter`/`Histogram`/`Gauge` in `telemetry.py`.
2. Add a `record_*` / `set_*` helper next to it.
3. Call it from the instrumentation point directly — don't wrap handlers.
4. Extend `tests/test_telemetry.py` to assert the metric family appears in the exposition output.

**Gotcha — config timing.** `create_app()` runs at import time, before `cli()` translates `--config` / `--db` / `--dev` into env vars. Do not gate `/metrics` or the HTTP middleware on `load_config()` called from `create_app()` — it sees a different config than `lifespan` will. The endpoint is always mounted at `/metrics`; disable externally (firewall / reverse proxy) if needed. Config-driven knobs (e.g. the queue-depth sampler interval) must be read inside `lifespan`, where the correct config is bound.

## Adding a new producer

1. Create `producers/my_producer.py`:
```python
from aiventbus.producers.base import BaseProducer

class MyProducer(BaseProducer):
    def __init__(self, bus, **config):
        self.bus = bus
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        self._task.cancel()

    @property
    def is_running(self):
        return self._running

    async def _loop(self):
        while self._running:
            # detect something, then:
            await self.bus.publish(EventCreate(
                topic="my.event",
                payload={"data": "..."},
                source="producer:my_producer",
            ))
            await asyncio.sleep(1)
```

2. Add config to `ProducersConfig` in `config.py`
3. Register in `ProducerManager.start_all()` in `producers/manager.py`

## Adding a new action type

1. Write a handler function:
```python
async def handle_my_action(data: dict) -> dict:
    # do something with data
    return {"result": "done"}
```

2. Register in `main.py` after executor creation:
```python
executor.register("my_action", handle_my_action)
```

3. Set trust mode in `core/policy.py` `_DEFAULT_TRUST`:
```python
"my_action": TrustMode.confirm,  # or auto, deny
```

4. Update the system prompt in `context_engine.py` to advertise it to agents.

## Adding a new API endpoint

1. Create `api/my_endpoint.py` with a FastAPI `APIRouter`
2. Add `init()` function for dependency injection
3. Wire in `main.py`:
   - Import and call `init()` in `lifespan()`
   - Include router in `create_app()`

## WebSocket protocol

Client connects to `/ws`, sends:
```json
{"action": "subscribe", "channels": ["events:*", "agents:*", "system"]}
```

Server broadcasts:
```json
{"channel": "events:user.query", "type": "event.new", "data": {...event...}}
```

Message types: `event.new`, `event.deduped`, `event.status`, `agent.status`, `agent.stream`, `agent.response`, `system.alert`, `action.pending`, `action.approved`, `action.denied`.

Channel matching supports wildcards: `events:*` matches `events:user.query`, `events:clipboard.text`, etc.

## Testing

```bash
# Start the daemon
python -m aiventbus

# In another terminal:
aibus status                              # check it's running
aibus query "hello"                       # test agent (needs agent + routing rule)
aibus events --limit 10                   # check events
aibus knowledge list --prefix system.     # check knowledge store

# Test policy engine
python -c "
from aiventbus.core.policy import PolicyEngine
pe = PolicyEngine()
print(pe.evaluate('shell_exec', {'command': 'ls -la'}))       # auto
print(pe.evaluate('shell_exec', {'command': 'rm -rf /'}))     # deny
print(pe.evaluate('file_write', {'path': '/tmp/test'}))       # confirm
"
```

## Widget development

The Tauri widget lives in `widget/`. Frontend is vanilla HTML/CSS/JS in `widget/src/`, Rust backend in `widget/src-tauri/`.

```bash
cd widget
cargo tauri dev    # hot-reload development mode
```

The widget connects to the daemon at `localhost:8420` via HTTP + WebSocket. It does not embed the daemon — both run independently.

## Project conventions

- Async everywhere (`async def`, `await`)
- Pydantic models for all data shapes (`models.py`)
- Repository pattern for DB access (`storage/repositories.py`)
- Global state initialized in `lifespan()`, accessed via module-level getters
- Config via dataclasses with YAML override (`config.py`)
- Logging via stdlib `logging` module
- IDs: `evt_`, `agent_`, `assign_`, `rule_`, `act_`, `tr_` prefixes
