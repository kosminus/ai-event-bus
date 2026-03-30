"""Events API — publish and query events."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from aiventbus.models import Event, EventCreate

router = APIRouter(prefix="/api/v1/events", tags=["events"])

# These get set by main.py at startup
_bus = None
_event_repo = None
_assignment_repo = None
_response_repo = None


def init(bus, event_repo, assignment_repo, response_repo):
    global _bus, _event_repo, _assignment_repo, _response_repo
    _bus = bus
    _event_repo = event_repo
    _assignment_repo = assignment_repo
    _response_repo = response_repo


@router.post("", response_model=Event)
async def publish_event(event_create: EventCreate):
    """Publish a new event onto the bus."""
    event = await _bus.publish(event_create)
    return event


@router.get("", response_model=list[Event])
async def list_events(
    topic: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List events with optional filters."""
    return await _event_repo.list(topic=topic, status=status, limit=limit, offset=offset)


@router.get("/{event_id}", response_model=Event)
async def get_event(event_id: str):
    """Get a single event by ID."""
    event = await _event_repo.get(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.get("/{event_id}/chain", response_model=list[Event])
async def get_event_chain(event_id: str):
    """Get the full event chain (parent + all descendants)."""
    event = await _event_repo.get(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return await _event_repo.get_chain(event_id)


@router.get("/{event_id}/assignments")
async def get_event_assignments(event_id: str):
    """Get all assignments for an event."""
    event = await _event_repo.get(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return await _assignment_repo.get_for_event(event_id)


@router.get("/{event_id}/responses")
async def get_event_responses(event_id: str):
    """Get all agent responses for an event."""
    event = await _event_repo.get(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return await _response_repo.get_for_event(event_id)
