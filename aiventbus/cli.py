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

    # Poll for response
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        r = client.get(f"/api/v1/events/{event_id}/responses")
        if r.status_code == 200:
            responses = r.json()
            if responses:
                resp = responses[0]
                parsed = resp.get("parsed_output")
                if parsed:
                    click.echo(f"\n{parsed.get('summary', '')}")
                    if parsed.get("proposed_actions"):
                        click.echo(f"\nActions: {json.dumps(parsed['proposed_actions'], indent=2)}")
                else:
                    click.echo(f"\n{resp.get('response_text', '')[:500]}")
                click.echo(f"\n(model: {resp.get('model_used')}, {resp.get('duration_ms')}ms)")
                return
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


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
