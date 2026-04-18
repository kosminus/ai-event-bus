"""Pydantic models for the AI Event Bus — tiered event schema."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _evt_id() -> str:
    return f"evt_{uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Enums ---

class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class EventStatus(str, Enum):
    received = "received"
    deduped = "deduped"
    routed = "routed"
    assigned = "assigned"
    processing = "processing"
    completed = "completed"
    expired = "expired"
    failed = "failed"
    compressed = "compressed"


class AssignmentStatus(str, Enum):
    pending = "pending"
    claimed = "claimed"
    running = "running"
    waiting_confirmation = "waiting_confirmation"
    resumable = "resumable"
    completed = "completed"
    failed = "failed"
    retry_wait = "retry_wait"


class AgentStatus(str, Enum):
    idle = "idle"
    processing = "processing"
    error = "error"
    disabled = "disabled"


class ProducerType(str, Enum):
    manual = "manual"
    cron = "cron"
    file_watcher = "file_watcher"
    log_tail = "log_tail"
    webhook = "webhook"
    fixture = "fixture"
    replay = "replay"


class ProducerStatus(str, Enum):
    running = "running"
    stopped = "stopped"
    error = "error"


class TrustMode(str, Enum):
    auto = "auto"
    confirm = "confirm"
    deny = "deny"


class ActionStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    executing = "executing"
    completed = "completed"
    denied = "denied"
    waiting_confirmation = "waiting_confirmation"
    failed = "failed"


class Lane(str, Enum):
    interactive = "interactive"
    critical = "critical"
    ambient = "ambient"


# --- Event Models (tiered schema) ---

class EventCreate(BaseModel):
    """Input model for creating an event. Only topic + payload required."""
    topic: str
    payload: dict[str, Any]
    # First-class optional
    priority: Priority = Priority.medium
    semantic_type: str | None = None
    dedupe_key: str | None = None
    parent_event: str | None = None
    output_topic: str | None = None
    context_refs: list[str] = Field(default_factory=list)
    memory_scope: str | None = None
    source: str | None = None
    trace_id: str | None = None
    expires_at: datetime | None = None
    max_retries: int = 0


def _trace_id() -> str:
    return f"tr_{uuid4().hex[:12]}"


class Event(BaseModel):
    """Full event as stored and transmitted."""
    id: str = Field(default_factory=_evt_id)
    timestamp: datetime = Field(default_factory=_now)
    topic: str
    payload: dict[str, Any]
    # First-class optional
    priority: Priority = Priority.medium
    semantic_type: str | None = None
    dedupe_key: str | None = None
    dedupe_count: int = 1
    parent_event: str | None = None
    output_topic: str | None = None
    context_refs: list[str] = Field(default_factory=list)
    memory_scope: str | None = None
    source: str | None = None
    trace_id: str | None = None
    # Lifecycle
    status: EventStatus = EventStatus.received
    producer_id: str | None = None
    expires_at: datetime | None = None
    max_retries: int = 0
    created_at: datetime = Field(default_factory=_now)


# --- Agent Models ---

class AgentCreate(BaseModel):
    """Input model for creating an agent."""
    name: str
    model: str
    system_prompt: str = "You are a helpful AI agent processing events from an event bus."
    description: str | None = None
    fallback_model: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    max_concurrent: int = 1
    queue_size: int = 50
    memory_scope: str | None = None
    reactive: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class Agent(BaseModel):
    """Full agent as stored."""
    id: str
    name: str
    model: str
    system_prompt: str = "You are a helpful AI agent processing events from an event bus."
    description: str | None = None
    fallback_model: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    max_concurrent: int = 1
    queue_size: int = 50
    memory_scope: str | None = None
    reactive: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatus = AgentStatus.idle
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# --- Producer Models ---

class ProducerCreate(BaseModel):
    """Input model for creating a producer."""
    name: str
    type: ProducerType
    config: dict[str, Any] = Field(default_factory=dict)
    default_topic: str | None = None
    default_semantic_type: str | None = None
    default_priority: Priority = Priority.medium


class Producer(BaseModel):
    """Full producer as stored."""
    id: str
    name: str
    type: ProducerType
    config: dict[str, Any] = Field(default_factory=dict)
    default_topic: str | None = None
    default_semantic_type: str | None = None
    default_priority: Priority = Priority.medium
    status: ProducerStatus = ProducerStatus.stopped
    error_message: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# --- Routing Rule Models ---

class RoutingRuleCreate(BaseModel):
    """Input model for creating a routing rule."""
    name: str
    topic_pattern: str | None = None
    semantic_type_pattern: str | None = None
    min_priority: Priority | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    consumer_id: str
    model_override: str | None = None
    token_budget_override: int | None = None
    priority_order: int = 100
    enabled: bool = True


class RoutingRule(BaseModel):
    """Full routing rule as stored."""
    id: str
    name: str
    topic_pattern: str | None = None
    semantic_type_pattern: str | None = None
    min_priority: Priority | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    consumer_id: str
    model_override: str | None = None
    token_budget_override: int | None = None
    priority_order: int = 100
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


# --- Assignment Models ---

class EventAssignment(BaseModel):
    """Tracks which agent is handling which event."""
    id: str
    event_id: str
    agent_id: str
    status: AssignmentStatus = AssignmentStatus.pending
    lane: Lane = Lane.ambient
    retry_count: int = 0
    model_used: str | None = None
    token_budget: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    conversation: dict[str, Any] | None = None
    iteration: int = 0
    waiting_action_id: str | None = None
    created_at: datetime = Field(default_factory=_now)


# --- Agent Response Models ---

class AgentResponseOutput(BaseModel):
    """Structured output from an LLM agent."""
    type: str  # analysis | action | escalate
    summary: str
    confidence: float | None = None
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list)


class AgentResponse(BaseModel):
    """Stored agent response."""
    id: str
    assignment_id: str
    agent_id: str
    event_id: str
    response_text: str
    parsed_output: AgentResponseOutput | None = None
    output_event_id: str | None = None
    model_used: str | None = None
    tokens_used: int | None = None
    duration_ms: int | None = None
    created_at: datetime = Field(default_factory=_now)


# --- Memory Models ---

class MemoryEntry(BaseModel):
    """A single memory entry for an agent."""
    id: int | None = None
    agent_id: str
    memory_scope: str
    role: str  # system | user | assistant
    content: str
    event_id: str | None = None
    token_count: int | None = None
    created_at: datetime = Field(default_factory=_now)


class PinnedFact(BaseModel):
    """A persistent fact for an agent."""
    id: int | None = None
    agent_id: str
    memory_scope: str
    content: str
    created_at: datetime = Field(default_factory=_now)


class MemoryKind(str, Enum):
    episodic = "episodic"
    semantic = "semantic"
    procedural = "procedural"


class MemoryRecord(BaseModel):
    """A distilled long-term memory record."""
    id: str
    kind: MemoryKind
    scope: str
    content: str
    summary: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    source_event_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    last_accessed_at: datetime | None = None
    access_count: int = 0
    expires_at: datetime | None = None


class MemoryRecordCreate(BaseModel):
    """Input model for creating a distilled long-term memory record."""
    kind: MemoryKind
    scope: str
    content: str
    summary: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    source_event_id: str | None = None
    expires_at: datetime | None = None


class MemoryRecordUpdate(BaseModel):
    """Patch model for updating an existing memory record."""
    importance: float = Field(ge=0.0, le=1.0)


# --- Knowledge Models ---

class KnowledgeEntry(BaseModel):
    """A durable fact stored in the knowledge store."""
    key: str
    value: str
    source: str | None = None
    updated_at: datetime = Field(default_factory=_now)


# --- Action Models ---

def _action_id() -> str:
    return f"act_{uuid4().hex[:10]}"


class PendingAction(BaseModel):
    """An action proposed by an agent, awaiting policy evaluation or user confirmation."""
    id: str = Field(default_factory=_action_id)
    assignment_id: str
    agent_id: str
    event_id: str
    action_type: str
    action_data: dict[str, Any] = Field(default_factory=dict)
    trust_mode: TrustMode = TrustMode.confirm
    status: ActionStatus = ActionStatus.pending
    policy_reason: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=_now)
    resolved_at: datetime | None = None
