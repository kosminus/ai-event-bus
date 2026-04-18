"""Distilled memory API — CRUD and search for long-term recalled experience."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from aiventbus.models import MemoryRecord, MemoryRecordCreate, MemoryRecordUpdate
from aiventbus.storage.repositories import MemoryStore

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])

_repo: MemoryStore | None = None


def init(memory_store: MemoryStore) -> None:
    global _repo
    _repo = memory_store


@router.get("", response_model=list[MemoryRecord])
async def list_memories(
    scope: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    q: str | None = Query(default=None, description="FTS query"),
):
    return await _repo.list(scope=scope, kind=kind, tag=tag, limit=limit, q=q)


@router.post("", response_model=MemoryRecord)
async def create_memory(data: MemoryRecordCreate):
    return await _repo.add(data)


@router.get("/{memory_id}", response_model=MemoryRecord)
async def get_memory(memory_id: str):
    memory = await _repo.get(memory_id)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.patch("/{memory_id}", response_model=MemoryRecord)
async def update_memory(memory_id: str, data: MemoryRecordUpdate):
    memory = await _repo.update(memory_id, data)
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    deleted = await _repo.delete(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True}
