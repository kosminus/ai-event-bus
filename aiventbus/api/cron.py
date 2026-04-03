"""Cron jobs API — manage scheduled event emissions at runtime."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/cron", tags=["cron"])

_producer_manager = None


def init(producer_manager):
    global _producer_manager
    _producer_manager = producer_manager


class CronJobCreate(BaseModel):
    name: str
    expression: str  # cron expression or interval shorthand (5m, 1h)
    topic: str
    payload: dict[str, Any] = {}
    priority: str = "medium"


def _get_cron_producer():
    from aiventbus.producers.cron import CronProducer

    if not _producer_manager:
        raise HTTPException(status_code=503, detail="Cron producer not initialized")
    producer = _producer_manager.get("cron")
    if not producer or not producer.is_running:
        raise HTTPException(status_code=503, detail="Cron producer is disabled — enable it first")
    return producer


@router.get("/jobs")
async def list_cron_jobs():
    """List all scheduled cron jobs with next fire times."""
    producer = _get_cron_producer()
    return producer.list_jobs()


@router.post("/jobs")
async def add_cron_job(job: CronJobCreate):
    """Add a new cron job at runtime."""
    from aiventbus.producers.cron import CronJob, _parse_trigger

    producer = _get_cron_producer()

    # Validate the expression before adding
    try:
        _parse_trigger(job.expression)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cron_job = CronJob(
        name=job.name,
        expression=job.expression,
        topic=job.topic,
        payload=job.payload,
        priority=job.priority,
    )
    producer.add_job(cron_job)
    return {"name": job.name, "expression": job.expression, "topic": job.topic, "status": "scheduled"}


@router.delete("/jobs/{name}")
async def remove_cron_job(name: str):
    """Remove a cron job by name."""
    producer = _get_cron_producer()
    producer.remove_job(name)
    return {"name": name, "status": "removed"}
