# Getting Started

This guide walks you through installing aiventbus, creating your first agent, and having a conversation with it.

## Prerequisites

- **Linux (Ubuntu/Debian recommended)** — required. Deep OS integration via DBus, inotify, notify-send, and systemd. macOS and Windows are not supported.
- **Python 3.11+**
- **Ollama** running locally — install from [ollama.com](https://ollama.com), then pull a model:
  ```bash
  ollama pull gemma4:latest
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

On first run, the bus automatically seeds **8 default agents** (General Assistant, Clipboard Analyzer, File Watcher Agent, Notification Summarizer, Terminal Helper, System Log Analyst, Webhook Handler, Scheduled Task Agent) and **9 routing rules** mapping topic patterns to agents. You're ready to go immediately.

Check the auto-seeded system knowledge:

```bash
aibus knowledge list --prefix system.
```

You'll see the system already knows about your machine (hostname, GPU, memory, OS).

> **Note:** To disable auto-seeding, set `seed_defaults: false` in `config.yaml`. You can also create additional agents and routing rules manually via the API — see the [Developer Guide](developer-guide.md).

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
3. Default routing rule matched `user.*` to the General Assistant agent
4. Context engine built a prompt with:
   - Agent's system prompt
   - System facts from knowledge store (your GPU, OS, memory)
   - The question
5. Agent called Ollama (gemma4), streamed the response
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

## Routing rules

The default seeder already creates routing rules for all built-in producers (`clipboard.*`, `fs.*`, `notification.*`, `terminal.*`, `syslog.*`, `webhook.*`, `cron.*`). To add custom rules:

```bash
# Route custom topic to a specific agent
curl -X POST http://localhost:8420/api/v1/routing-rules \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "my custom rule",
    "topic_pattern": "my.custom.*",
    "consumer_id": "<agent_id>"
  }'
```

## Use the classifier (optional)

Instead of writing routing rules for everything, enable the LLM classifier. It automatically decides which agent should handle unmatched events:

```yaml
classifier:
  enabled: true
  model: "gemma4:latest"
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
