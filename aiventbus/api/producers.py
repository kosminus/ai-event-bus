"""Producers API — list, enable, disable event producers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aiventbus.producers.registry import get_spec

router = APIRouter(prefix="/api/v1/producers", tags=["producers"])

_producer_manager = None


def init(producer_manager):
    global _producer_manager
    _producer_manager = producer_manager


@router.get("")
async def list_producers():
    """List all registered producers with running status + capability map."""
    return _producer_manager.list_all()


@router.post("/{name}/enable")
async def enable_producer(name: str):
    """Start a producer by name."""
    if get_spec(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown producer: {name}")
    ok = await _producer_manager.enable(name)
    if not ok:
        # Surface the runnable reason if the producer is unavailable here.
        for entry in _producer_manager.list_all():
            if entry["name"] == name:
                reason = entry.get("unavailable_reason")
                detail = (
                    f"Failed to start producer '{name}'"
                    if not reason
                    else f"Producer '{name}' unavailable: {reason}"
                )
                raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=400, detail=f"Failed to start producer: {name}")
    return {"name": name, "running": True}


@router.post("/{name}/disable")
async def disable_producer(name: str):
    """Stop a producer by name."""
    if get_spec(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown producer: {name}")
    await _producer_manager.disable(name)
    return {"name": name, "running": False}
