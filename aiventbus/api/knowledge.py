"""Knowledge store API — CRUD for durable key-value facts."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aiventbus.storage.repositories import KnowledgeRepository

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

_repo: KnowledgeRepository | None = None


def init(knowledge_repo: KnowledgeRepository) -> None:
    global _repo
    _repo = knowledge_repo


class KnowledgeSetRequest(BaseModel):
    value: str
    source: str | None = None


@router.get("")
async def list_knowledge(prefix: str | None = None, limit: int = 100):
    if prefix:
        return await _repo.scan(prefix)
    return await _repo.list_all(limit=limit)


@router.get("/{key:path}")
async def get_knowledge(key: str):
    entry = await _repo.get(key)
    if not entry:
        raise HTTPException(404, f"Key not found: {key}")
    return entry


@router.put("/{key:path}")
async def set_knowledge(key: str, body: KnowledgeSetRequest):
    await _repo.set(key, body.value, source=body.source)
    return {"key": key, "status": "stored"}


@router.delete("/{key:path}")
async def delete_knowledge(key: str):
    deleted = await _repo.delete(key)
    if not deleted:
        raise HTTPException(404, f"Key not found: {key}")
    return {"key": key, "status": "deleted"}
