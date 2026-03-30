"""Agents API — CRUD for LLM agent consumers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aiventbus.models import Agent, AgentCreate

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_agent_repo = None
_memory_repo = None


def init(agent_repo, memory_repo):
    global _agent_repo, _memory_repo
    _agent_repo = agent_repo
    _memory_repo = memory_repo


def _get_agent_manager():
    from aiventbus.main import get_agent_manager
    return get_agent_manager()


@router.get("", response_model=list[Agent])
async def list_agents():
    return await _agent_repo.list()


@router.post("", response_model=Agent)
async def create_agent(data: AgentCreate):
    agent = await _agent_repo.create(data)
    # Auto-start the consumer
    mgr = _get_agent_manager()
    if mgr:
        await mgr.start_agent(agent.id)
    return agent


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: str):
    agent = await _agent_repo.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/{agent_id}", response_model=Agent)
async def update_agent(agent_id: str, data: dict):
    agent = await _agent_repo.update(agent_id, data)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    mgr = _get_agent_manager()
    if mgr:
        await mgr.stop_agent(agent_id)
    if not await _agent_repo.delete(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"deleted": True}


@router.post("/{agent_id}/enable")
async def enable_agent(agent_id: str):
    agent = await _agent_repo.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _agent_repo.update_status(agent_id, "idle")
    mgr = _get_agent_manager()
    if mgr:
        await mgr.start_agent(agent_id)
    return {"status": "idle"}


@router.post("/{agent_id}/disable")
async def disable_agent(agent_id: str):
    agent = await _agent_repo.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _agent_repo.update_status(agent_id, "disabled")
    mgr = _get_agent_manager()
    if mgr:
        await mgr.stop_agent(agent_id)
    return {"status": "disabled"}


@router.get("/{agent_id}/memory")
async def get_agent_memory(agent_id: str):
    agent = await _agent_repo.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    scope = agent.memory_scope or agent.id
    memory = await _memory_repo.get_recent(agent_id, scope)
    pinned = await _memory_repo.get_pinned_facts(agent_id, scope)
    return {"memory": memory, "pinned_facts": pinned}


@router.delete("/{agent_id}/memory")
async def clear_agent_memory(agent_id: str):
    agent = await _agent_repo.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    scope = agent.memory_scope or agent.id
    await _memory_repo.clear(agent_id, scope)
    return {"cleared": True}
