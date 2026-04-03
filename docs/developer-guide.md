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
