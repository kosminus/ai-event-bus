"""Actions API — confirmation queue for agent-proposed actions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from aiventbus.core.executor import Executor
from aiventbus.core.bus import EventBus, WebSocketHub
from aiventbus.models import ActionStatus
from aiventbus.storage.repositories import AssignmentRepository, PendingActionRepository

router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

_action_repo: PendingActionRepository | None = None
_executor: Executor | None = None
_bus: EventBus | None = None
_ws_hub: WebSocketHub | None = None
_assignment_repo: AssignmentRepository | None = None
_agent_manager: Any = None


def init(
    action_repo: PendingActionRepository,
    executor: Executor,
    bus: EventBus,
    ws_hub: WebSocketHub,
    assignment_repo: AssignmentRepository | None = None,
    agent_manager: Any = None,
) -> None:
    global _action_repo, _executor, _bus, _ws_hub, _assignment_repo, _agent_manager
    _action_repo = action_repo
    _executor = executor
    _bus = bus
    _ws_hub = ws_hub
    _assignment_repo = assignment_repo
    _agent_manager = agent_manager


async def _resume_if_waiting(action_id: str) -> None:
    """If an assignment is suspended waiting on this action, wake it up."""
    if not (_assignment_repo and _agent_manager):
        return
    assignment = await _assignment_repo.find_by_waiting_action(action_id)
    if not assignment:
        return
    await _agent_manager.resume_assignment(assignment.id, assignment.agent_id)


@router.get("/pending")
async def list_pending(limit: int = 50):
    return await _action_repo.list_pending(limit=limit)


@router.get("/history")
async def list_history(limit: int = 50):
    return await _action_repo.list_recent(limit=limit)


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

    # Execute the approved action. If the executor raises, we still need to
    # record a failure result and resume the waiting assignment — otherwise
    # the tool-use loop stays suspended forever with no way to recover.
    try:
        result = await _executor.execute(action.action_type, action.action_data)
        status = ActionStatus.completed
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
        status = ActionStatus.failed

    await _action_repo.update_result(action_id, status, result)

    await _ws_hub.broadcast("system", "action.approved", {
        "action_id": action_id,
        "action_type": action.action_type,
        "result": result,
        "status": status.value,
    })

    await _resume_if_waiting(action_id)

    if status == ActionStatus.failed:
        raise HTTPException(500, f"Action execution failed: {result['error']}")

    return {"action_id": action_id, "status": status.value, "result": result}


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

    # Resume any assignment that was suspended waiting on this action
    await _resume_if_waiting(action_id)

    return {"action_id": action_id, "status": "denied"}
