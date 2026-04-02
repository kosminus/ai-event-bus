"""Producers API — list, enable, disable event producers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/producers", tags=["producers"])

_producer_manager = None


def init(producer_manager):
    global _producer_manager
    _producer_manager = producer_manager


@router.get("")
async def list_producers():
    """List all known producers with their running status."""
    return _producer_manager.list_all()


@router.post("/{name}/enable")
async def enable_producer(name: str):
    """Start a producer by name."""
    from aiventbus.producers.manager import PRODUCER_REGISTRY

    if name not in PRODUCER_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown producer: {name}")
    ok = await _producer_manager.enable(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Failed to start producer: {name}")
    return {"name": name, "running": True}


@router.post("/{name}/disable")
async def disable_producer(name: str):
    """Stop a producer by name."""
    from aiventbus.producers.manager import PRODUCER_REGISTRY

    if name not in PRODUCER_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown producer: {name}")
    await _producer_manager.disable(name)
    return {"name": name, "running": False}
