"""Actions API — confirmation queue for agent-proposed actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aiventbus.core.executor import Executor
from aiventbus.core.bus import EventBus, WebSocketHub
from aiventbus.models import ActionStatus
from aiventbus.storage.repositories import PendingActionRepository

router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

_action_repo: PendingActionRepository | None = None
_executor: Executor | None = None
_bus: EventBus | None = None
_ws_hub: WebSocketHub | None = None


def init(
    action_repo: PendingActionRepository,
    executor: Executor,
    bus: EventBus,
    ws_hub: WebSocketHub,
) -> None:
    global _action_repo, _executor, _bus, _ws_hub
    _action_repo = action_repo
    _executor = executor
    _bus = bus
    _ws_hub = ws_hub


@router.get("/pending")
async def list_pending(limit: int = 50):
    return await _action_repo.list_pending(limit=limit)


@router.get("/{action_id}")
async def get_action(action_id: str):
    action = await _action_repo.get(action_id)
    if not action:
        raise HTTPException(404, "Action not found")
    return action


@router.post("/{action_id}/approve")
async def approve_action(action_id: str):
    action = await _action_repo.approve(action_id)
    if not action:
        raise HTTPException(404, "Action not found or not awaiting confirmation")

    # Execute the approved action
    result = await _executor.execute(action.action_type, action.action_data)
    await _action_repo.update_result(action_id, ActionStatus.completed, result)

    # Broadcast approval
    await _ws_hub.broadcast("system", "action.approved", {
        "action_id": action_id,
        "action_type": action.action_type,
        "result": result,
    })

    return {"action_id": action_id, "status": "completed", "result": result}


@router.post("/{action_id}/deny")
async def deny_action(action_id: str, reason: str | None = None):
    action = await _action_repo.deny(action_id, reason)
    if not action:
        raise HTTPException(404, "Action not found or not awaiting confirmation")

    # Broadcast denial
    await _ws_hub.broadcast("system", "action.denied", {
        "action_id": action_id,
        "action_type": action.action_type,
        "reason": reason,
    })

    return {"action_id": action_id, "status": "denied"}
