"""Assignments API — admin endpoints for the event-assignment queue.

The regular read paths for assignments already live under ``/api/v1/events``
and ``/api/v1/actions``; this module adds the big-hammer operations needed
when an agent / producer combination has filled the queue faster than
humans can review it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from aiventbus.core.bus import WebSocketHub
from aiventbus.storage.repositories import AssignmentRepository, PendingActionRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assignments", tags=["assignments"])

_assignment_repo: AssignmentRepository | None = None
_action_repo: PendingActionRepository | None = None
_ws_hub: WebSocketHub | None = None


def init(
    assignment_repo: AssignmentRepository,
    action_repo: PendingActionRepository,
    ws_hub: WebSocketHub,
) -> None:
    global _assignment_repo, _action_repo, _ws_hub
    _assignment_repo = assignment_repo
    _action_repo = action_repo
    _ws_hub = ws_hub


@router.post("/cancel-pending")
async def cancel_pending(agent_id: str | None = None, reason: str | None = None):
    """Drain the queue of all non-running assignments.

    Flips ``pending`` / ``claimed`` / ``waiting_confirmation`` / ``resumable``
    / ``retry_wait`` rows to ``failed`` with a stamped reason. ``running``
    rows are left alone — racing an agent's in-flight Ollama call would
    leave us without a clean way to stop it. Any ``waiting_confirmation``
    assignments have their linked ``pending_actions`` cascade-denied so
    the Approvals queue doesn't end up with orphaned rows pointing at
    dead assignments.

    Scope with ``agent_id`` to target one noisy agent. Leave it unset to
    drain everything.
    """
    if _assignment_repo is None or _action_repo is None:
        raise HTTPException(503, "Assignments API not initialized")

    stamp = reason or (
        f"cancelled via drain for agent {agent_id}" if agent_id else "cancelled via drain"
    )
    cancelled_ids = await _assignment_repo.cancel_pending(agent_id=agent_id, reason=stamp)

    # Cascade-deny approvals linked to the cancelled assignments. We scope
    # by agent_id when set — that already narrows far enough without a
    # per-assignment-id lookup.
    denied_action_ids = await _action_repo.list_pending_ids(agent_id=agent_id)
    cascaded: list[str] = []
    for action_id in denied_action_ids:
        try:
            action = await _action_repo.deny(action_id, stamp)
            if action is None:
                continue
            cascaded.append(action_id)
            if _ws_hub is not None:
                await _ws_hub.broadcast("system", "action.denied", {
                    "action_id": action_id,
                    "action_type": action.action_type,
                    "reason": stamp,
                })
        except Exception:
            logger.exception("drain: cascade-deny failed for action %s", action_id)

    if _ws_hub is not None and cancelled_ids:
        await _ws_hub.broadcast("system", "assignments.cancelled", {
            "count": len(cancelled_ids),
            "agent_id": agent_id,
            "reason": stamp,
        })

    return {
        "cancelled_assignments": cancelled_ids,
        "cascaded_actions": cascaded,
        "agent_id": agent_id,
        "reason": stamp,
    }
