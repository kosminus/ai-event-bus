"""CLI interface for the AI Event Bus daemon.

Usage:
    aibus query "why is my machine slow?"
    aibus status
    aibus events [--topic clipboard.text] [--limit 20]
    aibus approve <action_id>
    aibus deny <action_id>
    aibus knowledge get <key>
    aibus knowledge set <key> <value>
    aibus knowledge list [--prefix system.]
    aibus trace <trace_id>
"""

from __future__ import annotations

import json
import sys
import time

import click
import httpx

DEFAULT_BASE = "http://localhost:8420"


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=120.0)


def _print_json(data, compact: bool = False):
    if compact:
        click.echo(json.dumps(data))
    else:
        click.echo(json.dumps(data, indent=2))


def _check_daemon(client: httpx.Client) -> bool:
    try:
        r = client.get("/api/v1/system/status")
        return r.status_code == 200
    except httpx.ConnectError:
        return False


@click.group()
@click.option("--url", default=DEFAULT_BASE, envvar="AIVENTBUS_URL", help="Daemon URL")
@click.pass_context
def cli(ctx, url: str):
    """AI Event Bus — local AI control plane CLI."""
    ctx.ensure_object(dict)
    ctx.obj["url"] = url


@cli.command()
@click.argument("text")
@click.option("--wait/--no-wait", default=True, help="Wait for agent response")
@click.option("--timeout", default=60, help="Timeout in seconds")
@click.pass_context
def query(ctx, text: str, wait: bool, timeout: int):
    """Send a query to the AI Event Bus and wait for a response."""
    client = _client(ctx.obj["url"])
    if not _check_daemon(client):
        click.echo("Error: daemon not running at " + ctx.obj["url"], err=True)
        sys.exit(1)

    # Publish user.query event
    r = client.post("/api/v1/events", json={
        "topic": "user.query",
        "payload": {"query": text},
        "priority": "high",
    })
    if r.status_code != 200:
        click.echo(f"Error publishing: {r.text}", err=True)
        sys.exit(1)

    event = r.json()
    event_id = event["id"]
    click.echo(f"Published {event_id} (trace: {event.get('trace_id', 'none')})")

    if not wait:
        return

    # Poll for response. The agent runs a tool-use loop — there may be many
    # intermediate responses; we surface the terminal one (no proposed_actions),
    # or if the loop is suspended on a confirm-gated action we tell the user.
    start = time.monotonic()
    announced_action_ids: set[str] = set()
    last_summary_printed: str | None = None
    while time.monotonic() - start < timeout:
        event_r = client.get(f"/api/v1/events/{event_id}")
        event_status = event_r.json().get("status") if event_r.status_code == 200 else None

        responses_r = client.get(f"/api/v1/events/{event_id}/responses")
        responses = responses_r.json() if responses_r.status_code == 200 else []

        # Stream intermediate summaries as the loop runs
        if responses:
            latest = responses[-1]
            parsed = latest.get("parsed_output") or {}
            summary = parsed.get("summary", "")
            if summary and summary != last_summary_printed:
                click.echo(f"\n{summary}")
                last_summary_printed = summary
            actions = parsed.get("proposed_actions") or []

            # Terminal: no more proposed actions and event is marked complete
            if event_status == "completed" and not actions:
                click.echo(f"\n(model: {latest.get('model_used')}, {latest.get('duration_ms')}ms)")
                return

        # Show new pending actions so the user knows to approve/deny
        pending_r = client.get("/api/v1/actions/pending")
        if pending_r.status_code == 200:
            for a in pending_r.json():
                if a.get("event_id") == event_id and a["id"] not in announced_action_ids:
                    announced_action_ids.add(a["id"])
                    click.echo(
                        f"\n[pending confirmation] {a['id']}  {a['action_type']}"
                        f"\n  {json.dumps(a.get('action_data', {}), indent=2)}"
                        f"\n  approve:  aibus approve {a['id']}"
                        f"\n  deny:     aibus deny {a['id']}"
                    )

        if event_status in ("failed", "expired"):
            click.echo(f"\nEvent ended with status: {event_status}", err=True)
            sys.exit(1)

        time.sleep(1)

    click.echo("Timeout waiting for response", err=True)
    sys.exit(1)


@cli.command()
@click.pass_context
def status(ctx):
    """Show daemon status, recent events, and agent state."""
    client = _client(ctx.obj["url"])
    if not _check_daemon(client):
        click.echo("Error: daemon not running at " + ctx.obj["url"], err=True)
        sys.exit(1)

    # System status
    r = client.get("/api/v1/system/status")
    s = r.json()
    click.echo("=== AI Event Bus ===")
    click.echo(f"Status:      {s['status']}")
    click.echo(f"Events:      {s['events_total']}")
    click.echo(f"Agents:      {s['agents_total']}")
    click.echo(f"Active jobs: {s['active_assignments']}")

    # Agents
    r = client.get("/api/v1/agents")
    agents = r.json()
    if agents:
        click.echo("\n=== Agents ===")
        for a in agents:
            click.echo(f"  {a['id']:30s} {a['status']:12s} model={a['model']}")

    # Recent events
    r = client.get("/api/v1/events?limit=10")
    events = r.json()
    if events:
        click.echo("\n=== Recent Events ===")
        for e in events:
            ts = e["created_at"][:19] if e.get("created_at") else ""
            click.echo(f"  {ts}  {e['id']:20s}  {e['topic']:30s}  {e['status']}")

    # Pending actions
    r = client.get("/api/v1/actions/pending")
    actions = r.json()
    if actions:
        click.echo(f"\n=== Pending Actions ({len(actions)}) ===")
        for a in actions:
            click.echo(f"  {a['id']}  {a['action_type']:15s}  from {a['agent_id']}")


@cli.command()
@click.option("--topic", default=None, help="Filter by topic")
@click.option("--status", "status_filter", default=None, help="Filter by status")
@click.option("--limit", default=20, help="Max events to show")
@click.pass_context
def events(ctx, topic: str | None, status_filter: str | None, limit: int):
    """List recent events."""
    client = _client(ctx.obj["url"])
    params = {"limit": limit}
    if topic:
        params["topic"] = topic
    if status_filter:
        params["status"] = status_filter
    r = client.get("/api/v1/events", params=params)
    if r.status_code != 200:
        click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)
    for e in r.json():
        ts = e["created_at"][:19] if e.get("created_at") else ""
        trace = e.get("trace_id", "")[:15] if e.get("trace_id") else ""
        click.echo(f"{ts}  {e['id']:20s}  {e['topic']:30s}  {e['status']:10s}  {trace}")


@cli.command()
@click.argument("action_id")
@click.pass_context
def approve(ctx, action_id: str):
    """Approve a pending action."""
    client = _client(ctx.obj["url"])
    r = client.post(f"/api/v1/actions/{action_id}/approve")
    if r.status_code != 200:
        click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)
    result = r.json()
    click.echo(f"Approved: {action_id}")
    if result.get("result"):
        _print_json(result["result"])


@cli.command()
@click.argument("action_id")
@click.option("--reason", default=None, help="Reason for denial")
@click.pass_context
def deny(ctx, action_id: str, reason: str | None):
    """Deny a pending action."""
    client = _client(ctx.obj["url"])
    params = {}
    if reason:
        params["reason"] = reason
    r = client.post(f"/api/v1/actions/{action_id}/deny", params=params)
    if r.status_code != 200:
        click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)
    click.echo(f"Denied: {action_id}")


@cli.command()
@click.argument("trace_id")
@click.pass_context
def trace(ctx, trace_id: str):
    """View all events in a trace."""
    client = _client(ctx.obj["url"])
    r = client.get(f"/api/v1/events/trace/{trace_id}")
    if r.status_code != 200:
        click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)
    events = r.json()
    click.echo(f"=== Trace {trace_id} ({len(events)} events) ===")
    for e in events:
        ts = e["created_at"][:19] if e.get("created_at") else ""
        parent = f" <- {e['parent_event'][:15]}" if e.get("parent_event") else ""
        click.echo(f"  {ts}  {e['id']:20s}  {e['topic']:30s}  {e['status']}{parent}")


@cli.group()
def knowledge():
    """Manage the knowledge store."""
    pass


@knowledge.command("get")
@click.argument("key")
@click.pass_context
def knowledge_get(ctx, key: str):
    """Get a knowledge entry by key."""
    client = _client(ctx.obj["url"])
    r = client.get(f"/api/v1/knowledge/{key}")
    if r.status_code == 404:
        click.echo(f"Not found: {key}", err=True)
        sys.exit(1)
    entry = r.json()
    click.echo(f"{entry['key']} = {entry['value']}")
    if entry.get("source"):
        click.echo(f"  (source: {entry['source']})")


@knowledge.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--source", default="cli", help="Source identifier")
@click.pass_context
def knowledge_set(ctx, key: str, value: str, source: str):
    """Set a knowledge entry."""
    client = _client(ctx.obj["url"])
    r = client.put(f"/api/v1/knowledge/{key}", json={"value": value, "source": source})
    if r.status_code != 200:
        click.echo(f"Error: {r.text}", err=True)
        sys.exit(1)
    click.echo(f"Stored: {key} = {value}")


@knowledge.command("list")
@click.option("--prefix", default=None, help="Filter by key prefix")
@click.pass_context
def knowledge_list(ctx, prefix: str | None):
    """List knowledge entries."""
    client = _client(ctx.obj["url"])
    params = {}
    if prefix:
        params["prefix"] = prefix
    r = client.get("/api/v1/knowledge", params=params)
    for entry in r.json():
        click.echo(f"  {entry['key']:40s} = {entry['value']}")


@cli.command("shell-hook")
@click.option("--install", is_flag=True, help="Add to your shell rc file automatically")
@click.pass_context
def shell_hook(ctx, install: bool):
    """Print the shell preexec hook script.

    Usage:
        eval "$(aibus shell-hook)"          # activate in current shell
        aibus shell-hook --install          # append to ~/.bashrc or ~/.zshrc
    """
    import os
    from pathlib import Path

    hook_path = Path(__file__).parent / "shell_hook.sh"
    hook_source = hook_path.read_text()

    if not install:
        click.echo(hook_source)
        return

    # Detect shell rc file
    shell = os.environ.get("SHELL", "/bin/bash")
    if "zsh" in shell:
        rc_file = Path.home() / ".zshrc"
    else:
        rc_file = Path.home() / ".bashrc"

    marker = '# AI Event Bus preexec hook'
    line = f'\n{marker}\neval "$(aibus shell-hook)"\n'

    # Check if already installed
    if rc_file.exists() and marker in rc_file.read_text():
        click.echo(f"Already installed in {rc_file}")
        return

    with open(rc_file, "a") as f:
        f.write(line)
    click.echo(f"Installed in {rc_file}")
    click.echo(f"Run: source {rc_file}")


@cli.command("install")
@click.option("--dev", is_flag=True,
              help="Dev install: symlink the Swift helper instead of copying, so rebuilds are picked up.")
@click.option("--build-helper", is_flag=True,
              help="macOS only: build bin/aiventbus-mac-helper (release) and install it.")
def install_cmd(dev: bool, build_helper: bool):
    """Install the daemon as a systemd user unit (Linux) or LaunchAgent (macOS)."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(message)s")
    from aiventbus.install import install as _install

    try:
        _install(dev=dev, build_helper=build_helper)
    except Exception as e:
        click.echo(f"Install failed: {e}", err=True)
        raise SystemExit(1)
    click.echo("Install complete.")


@cli.command("uninstall")
@click.option("--purge", is_flag=True,
              help="Also delete the config, data (DB), and log directories.")
def uninstall_cmd(purge: bool):
    """Disable and remove the installed service + helper binary."""
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(message)s")
    from aiventbus.install import uninstall as _uninstall

    try:
        _uninstall(purge=purge)
    except Exception as e:
        click.echo(f"Uninstall failed: {e}", err=True)
        raise SystemExit(1)
    click.echo("Uninstall complete.")


@cli.command("restart")
def restart_cmd():
    """Restart the installed daemon via the platform's service manager.

    Linux: `systemctl --user restart aiventbus.service`
    macOS: `launchctl kickstart -k gui/<uid>/com.aiventbus.daemon`

    Requires `aibus install` to have set up the service. A foreground
    `python -m aiventbus` run has no supervisor to restart, so stop
    the process and relaunch it instead.
    """
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(message)s")
    from aiventbus.install import restart as _restart, NoServiceInstalled

    try:
        msg = _restart()
    except NoServiceInstalled as e:
        click.echo(str(e), err=True)
        raise SystemExit(2)
    except Exception as e:
        click.echo(f"Restart failed: {e}", err=True)
        raise SystemExit(1)
    click.echo(msg)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
