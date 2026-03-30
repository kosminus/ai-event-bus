# AI Event Bus

A local-first intelligence bus for orchestrating multiple LLM agents via [Ollama](https://ollama.com). Event-driven architecture where producers emit events, routing rules match them to LLM consumers, and agent outputs flow back as new events.

```
producers → ingest → route → assign → context-build → agent-run → parse-output → emit back
```

## Why?

Existing AI agent frameworks (LangChain, CrewAI) are request/response. This is event-driven — like Kafka, but AI-native:

- **Token-aware context assembly** — not raw bytes, but structured prompts with memory and references
- **Structured agent output** — LLMs return typed JSON with actions, not free text
- **Chain reactions** — agent output becomes a new event that triggers other agents
- **Semantic routing** — route by topic pattern, semantic type, priority, not just string keys
- **All local** — runs on your machine via Ollama, free and private

## Quick start

**Prerequisites:** Python 3.11+, [Ollama](https://ollama.com) running locally

```bash
# Install
git clone <repo-url> && cd aiventbus
pip install -e .

# Run
python -m aiventbus
```

Open [http://localhost:8420](http://localhost:8420) for the dashboard. API docs at [http://localhost:8420/docs](http://localhost:8420/docs).

## Usage

### 1. Create an agent

```bash
curl -X POST http://localhost:8420/api/v1/agents \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "security scanner",
    "model": "gemma3:4b",
    "system_prompt": "You analyze security events and identify threats.",
    "capabilities": ["security"]
  }'
```

### 2. Create a routing rule

```bash
curl -X POST http://localhost:8420/api/v1/routing-rules \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "security events to scanner",
    "topic_pattern": "log.*",
    "semantic_type_pattern": "security.*",
    "consumer_id": "agent_security-scanner"
  }'
```

### 3. Publish an event

```bash
curl -X POST http://localhost:8420/api/v1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "log.error",
    "payload": {"message": "401 Unauthorized", "path": "/admin", "ip": "10.0.0.1"},
    "priority": "high",
    "semantic_type": "security.auth_failure"
  }'
```

The event gets routed to the security scanner agent, which calls Ollama, returns a structured analysis, and optionally emits follow-up events.

### 4. Or use the dashboard

Everything above can be done from the web UI — create agents, set up rules, publish events, watch agents process them in real-time.

## Event schema

Events use a tiered schema. Only `topic` and `payload` are required:

```json
{
  "topic": "log.error",
  "payload": {"message": "401 Unauthorized"},

  "priority": "high",
  "semantic_type": "security.auth_failure",
  "dedupe_key": "auth-401-/admin",
  "parent_event": "evt_abc123",
  "output_topic": "agent.security.response",
  "context_refs": ["evt_deploy_xyz"],
  "memory_scope": "security-agent"
}
```

## Agent output format

Agents return structured JSON:

```json
{
  "type": "analysis",
  "summary": "Repeated 401s suggest credential stuffing from 10.0.0.1",
  "confidence": 0.92,
  "proposed_actions": [
    {
      "action_type": "emit_event",
      "topic": "security.alert",
      "payload": {"severity": "high", "ip": "10.0.0.1"}
    },
    {
      "action_type": "alert",
      "message": "Possible credential stuffing attack detected"
    }
  ]
}
```

Action types: `emit_event` (chain reaction), `log`, `alert`.

## Architecture

```
┌─────────── PRODUCERS ────────────┐
│  manual (API/UI)                 │
│  cron, file_watcher, webhook     │  (planned)
│  log_tail, fixture, replay       │
└──────────────┬───────────────────┘
               ▼
        ┌──────────────┐
        │   EVENT BUS   │  SQLite persistence
        │               │  Dedupe, expiry
        │               │  Chain depth limits
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │   ROUTING     │  Glob patterns on topic + semantic_type
        │               │  Priority filtering, fan-out control
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  ASSIGNMENTS  │  Pull-based: agents claim work
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │ CONTEXT ENGINE│  Memory + pinned facts + refs
        │               │  Token-bounded prompt assembly
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  LLM AGENTS   │  Ollama streaming
        │               │  Structured output parsing
        └──────┬───────┘
               ▼
        back to EVENT BUS (chain reactions)
```

## Configuration

Optional `config.yaml` in project root:

```yaml
server:
  host: "0.0.0.0"
  port: 8420

ollama:
  base_url: "http://localhost:11434"
  default_model: "llama3.1:8b"
  request_timeout: 120

database:
  path: "./aiventbus.db"

bus:
  dedupe_window_seconds: 60
  max_fan_out: 3
  max_chain_depth: 10
  max_chain_budget: 20
```

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/events` | Publish event |
| `GET` | `/api/v1/events` | List events (filter by topic, status) |
| `GET` | `/api/v1/events/:id` | Event detail |
| `GET` | `/api/v1/events/:id/chain` | Full event chain (parent + descendants) |
| `GET` | `/api/v1/events/:id/assignments` | Assignments for event |
| `GET` | `/api/v1/events/:id/responses` | Agent responses for event |
| `POST` | `/api/v1/agents` | Create agent |
| `GET` | `/api/v1/agents` | List agents |
| `POST` | `/api/v1/agents/:id/enable` | Enable agent |
| `POST` | `/api/v1/agents/:id/disable` | Disable agent |
| `GET` | `/api/v1/agents/:id/memory` | Agent memory + pinned facts |
| `POST` | `/api/v1/routing-rules` | Create routing rule |
| `GET` | `/api/v1/routing-rules` | List rules |
| `GET` | `/api/v1/topics` | Topic stats |
| `GET` | `/api/v1/system/status` | Health check |
| `WS` | `/ws` | WebSocket (real-time events + agent streaming) |

## License

MIT
