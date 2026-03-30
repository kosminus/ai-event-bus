"""Routing Rules API — CRUD for event routing configuration."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from aiventbus.models import RoutingRule, RoutingRuleCreate

router = APIRouter(prefix="/api/v1/routing-rules", tags=["routing-rules"])

_rule_repo = None


def init(rule_repo):
    global _rule_repo
    _rule_repo = rule_repo


@router.get("", response_model=list[RoutingRule])
async def list_rules():
    return await _rule_repo.list()


@router.post("", response_model=RoutingRule)
async def create_rule(data: RoutingRuleCreate):
    return await _rule_repo.create(data)


@router.get("/{rule_id}", response_model=RoutingRule)
async def get_rule(rule_id: str):
    rule = await _rule_repo.get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str):
    if not await _rule_repo.delete(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"deleted": True}
