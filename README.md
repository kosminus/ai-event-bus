# AI Event Bus

A local-first AI control plane for **Linux** — an event-driven runtime that sits between your operating system and LLM agents via [Ollama](https://ollama.com). It gives your machine ambient intelligence: agents that watch, decide, and act on your behalf.

Built for **Ubuntu/Debian** and Linux desktops (GNOME, KDE). Deep OS integration via DBus, inotify, systemd, and desktop notifications. Runs entirely on your machine — no cloud, no API keys, no data leaves your box.

```
┌─────────────────────────────────────────────┐
│              USER (widget / CLI)             │
├─────────────────────────────────────────────┤
│           aiventbus control plane            │
│  events → routing → context → agents → acts  │
├─────────────────────────────────────────────┤
│        Linux OS (DBus, systemd, fs)          │
├─────────────────────────────────────────────┤
│        Hardware (GPU, disk, network)          │
└─────────────────────────────────────────────┘
```

## Why?

Existing AI agent frameworks (LangChain, CrewAI) are request/response. This is event-driven — agents are autonomous workers that react to the world:

- **System producers** — clipboard monitor, file watcher, DBus listener, terminal monitor give the daemon eyes and ears
- **Structured agent output** — LLMs return typed JSON with actions, not free text
- **Policy-gated execution** — agents propose actions (shell commands, file ops), a policy engine gates them (blocklist → allowlist → confirm)
- **Chain reactions** — agent output becomes a new event that triggers other agents
- **Priority lanes** — user queries never wait behind background clipboard events
- **Knowledge store** — durable key-value facts shared across all agents
- **Full traceability** — trace_id on every causal chain from trigger to action
- **All local** — runs on your machine via Ollama, free and private

## Platform support

| Platform | Status | Notes |
|----------|--------|-------|
| **Ubuntu/Debian** | Full support | Primary target. DBus, inotify, notify-send, systemd |
| **Arch/Fedora** | Should work | Same Linux APIs, untested |
| **macOS** | Partial | Core bus + agents work. No DBus, limited producers. Planned for future |
| **Windows** | Not supported | Different OS APIs entirely |

**Hardware:** Works on any machine with Ollama. Benefits from a GPU (NVIDIA recommended) for faster inference. Tested on RTX 5090 + RTX 6000 Pro with 70B+ models.

## Quick start

**Prerequisites:** Python 3.11+, Linux, [Ollama](https://ollama.com) running locally

```bash
# Install
git clone <repo-url> && cd aiventbus
pip install -e .

# Run the daemon
python -m aiventbus
```

Open [http://localhost:8420](http://localhost:8420) for the dashboard. API docs at [http://localhost:8420/docs](http://localhost:8420/docs).

## CLI

```bash
# Ask a question (publishes user.query, waits for agent response)
aibus query "what files were modified in the last 10 minutes?"

# Check status
aibus status

# List recent events
aibus events --topic clipboard.text --limit 20

# Manage pending actions
aibus approve <action_id>
aibus deny <action_id>

# Knowledge store
aibus knowledge list --prefix system.
aibus knowledge set user.pref.editor vscode
aibus knowledge get system.gpu
```

## Desktop Widget

A lightweight Tauri app (17MB) that connects to the running daemon:

```bash
cd widget
cargo tauri dev    # development
cargo tauri build  # production (.deb + AppImage)
```

Features: chat input with Ctrl+Space hotkey, tabbed activity feed (All/Files/Security/Approvals), action approval buttons, system tray icon, desktop notifications for critical events.

## Usage

### 1. Create an agent

```bash
curl -X POST http://localhost:8420/api/v1/agents \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "general assistant",
    "model": "gemma3:latest",
    "description": "General-purpose AI assistant",
    "system_prompt": "You are a helpful AI assistant. Answer concisely."
  }'
```

### 2. Create a routing rule

```bash
curl -X POST http://localhost:8420/api/v1/routing-rules \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "user queries",
    "topic_pattern": "user.*",
    "consumer_id": "agent_general-assistant"
  }'
```

### 3. Ask a question

```bash
aibus query "why is my machine slow?"
```

Or publish any event:

```bash
curl -X POST http://localhost:8420/api/v1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "log.error",
    "payload": {"message": "401 Unauthorized", "path": "/admin"},
    "priority": "high",
    "semantic_type": "security.auth_failure"
  }'
```

### 4. Classifier fallback

If no routing rule matches, an optional LLM classifier routes the event to the best agent automatically. Enable in `config.yaml`:

```yaml
classifier:
  enabled: true
  model: "gemma3:latest"
```

## Linux integration

aiventbus hooks into the OS to observe and act:

| Integration | How | What it does |
|-------------|-----|-------------|
| **Clipboard** | `wl-paste` / `xclip` | Monitors clipboard changes, agents can analyze copied text (stack traces, URLs, code) |
| **File system** | `inotify` via watchfiles | Watches directories (Downloads, Documents), agents can triage/organize files |
| **Desktop notifications** | DBus `org.freedesktop.Notifications` | Captures notifications from other apps, sends AI-generated notifications via `notify-send` |
| **Session events** | DBus `org.freedesktop.login1` | Detects screen lock/unlock for context-aware behavior |
| **Terminal** | Shell history monitoring | Watches bash/zsh history, agents can detect errors and suggest fixes |
| **Shell commands** | `asyncio.subprocess` | Agents propose commands, policy engine gates them, executor runs approved ones |
| **File operations** | Python pathlib | Agents can read/write/delete files (with policy confirmation) |
| **App launching** | `xdg-open` | Agents can open URLs and files in default applications |

## Event schema

Events use a tiered schema. Only `topic` and `payload` are required:

```json
{
  "topic": "log.error",
  "payload": {"message": "401 Unauthorized"},

  "priority": "high",
  "semantic_type": "security.auth_failure",
  "dedupe_key": "auth-401-/admin",
  "trace_id": "tr_abc123def456",
  "parent_event": "evt_abc123",
  "output_topic": "agent.security.response",
  "context_refs": ["evt_deploy_xyz"],
  "memory_scope": "security-agent"
}
```

## Agent output format

Agents return structured JSON with action proposals:

```json
{
  "type": "analysis",
  "summary": "Repeated 401s suggest credential stuffing from 10.0.0.1",
  "confidence": 0.92,
  "proposed_actions": [
    {
      "action_type": "shell_exec",
      "command": "last -i | grep 10.0.0.1"
    },
    {
      "action_type": "notify",
      "title": "Security Alert",
      "message": "Possible credential stuffing attack detected"
    },
    {
      "action_type": "set_knowledge",
      "key": "security.last_alert",
      "value": "credential stuffing from 10.0.0.1"
    }
  ]
}
```

Action types: `emit_event`, `log`, `alert`, `notify`, `shell_exec`, `file_read`, `file_write`, `file_delete`, `open_app`, `set_knowledge`, `get_knowledge`.

Actions go through the policy engine: auto-approved (safe commands), confirm (needs user approval), or deny (blocked patterns like `rm -rf /`, `sudo`).

## Architecture

```
┌─────────── PRODUCERS ─────────────┐
│  clipboard, file_watcher, terminal │
│  dbus_listener, manual (API/UI)    │
└──────────────┬─────────────────────┘
               ▼
        ┌──────────────┐
        │   EVENT BUS   │  SQLite persistence, dedupe, chain limits
        └──────┬───────┘
               ▼
      ┌────────────────────┐
      │   PRIORITY ROUTER   │  3 lanes: interactive / critical / ambient
      │                      │  Static rules + classifier fallback
      └────────┬────────────┘
               ▼
      ┌────────────────────┐
      │  CONTEXT ENGINE     │  Memory + pinned facts + knowledge store
      │                      │  Token-bounded prompt assembly
      └────────┬────────────┘
               ▼
      ┌────────────────────┐
      │    LLM AGENTS       │  Ollama streaming, structured output
      └────────┬────────────┘
               ▼
      ┌────────────────────┐
      │   POLICY ENGINE     │  Blocklist → allowlist → trust modes
      └────────┬────────────┘
               ▼
      ┌────────────────────┐
      │     EXECUTOR        │  Shell, filesystem, notifications
      └────────┬────────────┘
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

bus:
  dedupe_window_seconds: 60
  max_fan_out: 3
  max_chain_depth: 10
  max_chain_budget: 20

producers:
  clipboard_enabled: true
  file_watcher_enabled: false
  file_watcher_paths: ["~/Downloads", "~/Documents"]
  dbus_enabled: false
  terminal_monitor_enabled: false

classifier:
  enabled: false
  model: "gemma3:latest"

policy:
  trust_overrides: {}
  shell_timeout_seconds: 30

lanes:
  interactive_prefixes: ["user."]
  critical_prefixes: ["security.", "system.failure"]
```

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/events` | Publish event |
| `GET` | `/api/v1/events` | List events (filter by topic, status) |
| `GET` | `/api/v1/events/:id` | Event detail |
| `GET` | `/api/v1/events/:id/chain` | Full event chain |
| `GET` | `/api/v1/events/:id/assignments` | Assignments for event |
| `GET` | `/api/v1/events/:id/responses` | Agent responses for event |
| `GET` | `/api/v1/events/trace/:trace_id` | All events in a trace |
| `POST` | `/api/v1/agents` | Create agent |
| `GET` | `/api/v1/agents` | List agents |
| `POST` | `/api/v1/agents/:id/enable` | Enable agent |
| `POST` | `/api/v1/agents/:id/disable` | Disable agent |
| `GET` | `/api/v1/agents/:id/memory` | Agent memory |
| `POST` | `/api/v1/routing-rules` | Create routing rule |
| `GET` | `/api/v1/routing-rules` | List rules |
| `GET` | `/api/v1/actions/pending` | List pending actions |
| `POST` | `/api/v1/actions/:id/approve` | Approve action |
| `POST` | `/api/v1/actions/:id/deny` | Deny action |
| `GET` | `/api/v1/knowledge` | List knowledge (with prefix filter) |
| `PUT` | `/api/v1/knowledge/:key` | Set knowledge entry |
| `GET` | `/api/v1/knowledge/:key` | Get knowledge entry |
| `GET` | `/api/v1/topics` | Topic stats |
| `GET` | `/api/v1/system/status` | Health check |
| `WS` | `/ws` | WebSocket (real-time events + agent streaming) |

## License

MIT
