# Getting Started

This guide walks you through installing aiventbus, creating your first agent, and having a conversation with it.

## Prerequisites

- **Python 3.11+**
- **Ollama** running locally — install from [ollama.com](https://ollama.com), then pull a model:
  ```bash
  ollama pull gemma3:latest
  ```

## Installation

```bash
git clone <repo-url>
cd aiventbus
pip install -e .
```

## Start the daemon

```bash
python -m aiventbus
```

You should see:
```
AI Event Bus started on http://0.0.0.0:8420
Ollama connected. Available models: [...]
Clipboard producer started
```

The daemon is now running. Open [http://localhost:8420](http://localhost:8420) to see the web dashboard.

## Create your first agent

An agent is an LLM that processes events. Let's create one:

```bash
aibus knowledge list --prefix system.
```

You'll see the system already knows about your machine (hostname, GPU, memory, OS). Now create an agent:

```bash
curl -X POST http://localhost:8420/api/v1/agents \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "assistant",
    "model": "gemma3:latest",
    "description": "General-purpose assistant for answering questions",
    "system_prompt": "You are a helpful AI assistant running on the user machine. Answer concisely."
  }'
```

## Create a routing rule

Tell the bus which events go to your agent:

```bash
curl -X POST http://localhost:8420/api/v1/routing-rules \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "user queries to assistant",
    "topic_pattern": "user.*",
    "consumer_id": "agent_assistant"
  }'
```

## Ask a question

Using the CLI:

```bash
aibus query "what GPU do I have?"
```

The agent will read your system facts from the knowledge store and answer based on your actual hardware.

You can also ask from the web dashboard (publish an event with topic `user.query`) or from the desktop widget if installed.

## What just happened?

```
1. You typed a question
2. CLI published a user.query event to the bus
3. Routing rule matched it to your assistant agent
4. Context engine built a prompt with:
   - Agent's system prompt
   - System facts from knowledge store (your GPU, OS, memory)
   - The question
5. Agent called Ollama (gemma3), streamed the response
6. Response parsed as structured JSON
7. CLI displayed the summary
```

The event, assignment, and response are all persisted in SQLite. You can see the full trace:

```bash
aibus events --limit 5
```

## Enable more producers

By default, only the clipboard monitor runs. Edit `config.yaml` to enable more:

```yaml
producers:
  clipboard_enabled: true
  file_watcher_enabled: true
  file_watcher_paths:
    - ~/Downloads
    - ~/Documents
  terminal_monitor_enabled: true
```

Restart the daemon. Now file changes in Downloads and terminal commands will flow through the bus.

## Add routing rules for producers

```bash
# Route file events to a triage agent
curl -X POST http://localhost:8420/api/v1/routing-rules \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "file events",
    "topic_pattern": "fs.*",
    "consumer_id": "agent_assistant"
  }'

# Route clipboard events
curl -X POST http://localhost:8420/api/v1/routing-rules \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "clipboard events",
    "topic_pattern": "clipboard.*",
    "consumer_id": "agent_assistant"
  }'
```

## Use the classifier (optional)

Instead of writing routing rules for everything, enable the LLM classifier. It automatically decides which agent should handle unmatched events:

```yaml
classifier:
  enabled: true
  model: "gemma3:latest"
```

## Desktop widget

If you have Rust installed, build and run the widget:

```bash
cd widget
cargo tauri dev
```

A compact window appears with a chat input (Ctrl+Space to focus), activity feed, and action approval buttons. It connects to the running daemon via WebSocket.

## Next steps

- Create specialized agents (security scanner, code reviewer, file organizer)
- Set up routing rules with priority filtering (`min_priority: high`)
- Use the knowledge store to teach agents about your projects: `aibus knowledge set project.main.lang python`
- Check the [Developer Guide](developer-guide.md) to understand the internals
