"""Cron producer — publishes events on a schedule.

Supports standard cron expressions (``*/5 * * * *``) and simple interval
shorthand (``5m``, ``1h``, ``1d``).  Schedules are defined in config or
managed at runtime via the cron API.

Uses APScheduler (already a project dependency) for reliable scheduling.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from aiventbus.core.bus import EventBus
from aiventbus.models import EventCreate, Priority
from aiventbus.producers.base import BaseProducer

logger = logging.getLogger(__name__)

# Simple interval pattern: number + unit (s/m/h/d)
_INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)

_UNIT_MAP = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _parse_trigger(expression: str) -> CronTrigger | IntervalTrigger:
    """Parse a cron expression or interval shorthand into an APScheduler trigger."""
    m = _INTERVAL_RE.match(expression.strip())
    if m:
        value, unit = int(m.group(1)), m.group(2).lower()
        return IntervalTrigger(**{_UNIT_MAP[unit]: value})

    # Standard 5-field cron: minute hour day month day_of_week
    parts = expression.strip().split()
    if len(parts) == 5:
        return CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )

    raise ValueError(f"Invalid schedule expression: {expression!r}  (use cron '*/5 * * * *' or interval '5m')")


class CronJob:
    """A single scheduled event emission."""

    def __init__(self, name: str, expression: str, topic: str,
                 payload: dict[str, Any] | None = None,
                 priority: str = "medium"):
        self.name = name
        self.expression = expression
        self.topic = topic
        self.payload = payload or {}
        self.priority = priority


class CronProducer(BaseProducer):
    """Publishes events on a cron/interval schedule."""

    def __init__(
        self,
        bus: EventBus,
        jobs: list[dict[str, Any]] | None = None,
        timezone: str = "UTC",
    ):
        self.bus = bus
        self.timezone = timezone
        self._running = False
        self._scheduler: AsyncIOScheduler | None = None
        # Parse job configs into CronJob objects
        self._jobs: list[CronJob] = []
        for j in (jobs or []):
            try:
                self._jobs.append(CronJob(
                    name=j.get("name", j.get("topic", "unnamed")),
                    expression=j["expression"],
                    topic=j["topic"],
                    payload=j.get("payload", {}),
                    priority=j.get("priority", "medium"),
                ))
            except (KeyError, ValueError) as e:
                logger.warning("Skipping invalid cron job config: %s (%s)", j, e)

    async def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=self.timezone)

        for job in self._jobs:
            self._add_job_to_scheduler(job)

        self._scheduler.start()
        self._running = True
        logger.info(
            "Cron producer started with %d job(s) (tz=%s)",
            len(self._jobs), self.timezone,
        )

    async def stop(self) -> None:
        self._running = False
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        logger.info("Cron producer stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    def _add_job_to_scheduler(self, job: CronJob) -> None:
        """Register a CronJob with the APScheduler instance."""
        try:
            trigger = _parse_trigger(job.expression)
        except ValueError as e:
            logger.error("Cannot schedule job %r: %s", job.name, e)
            return

        self._scheduler.add_job(
            self._fire_event,
            trigger=trigger,
            args=[job],
            id=f"cron_{job.name}",
            replace_existing=True,
            name=job.name,
        )
        logger.debug("Scheduled job: %s (%s) → %s", job.name, job.expression, job.topic)

    async def _fire_event(self, job: CronJob) -> None:
        """Publish the scheduled event to the bus."""
        payload = {
            **job.payload,
            "cron_job": job.name,
            "schedule": job.expression,
            "fired_at": datetime.utcnow().isoformat(),
        }
        pri = Priority(job.priority) if job.priority in Priority.__members__ else Priority.medium

        await self.bus.publish(
            EventCreate(
                topic=job.topic,
                payload=payload,
                priority=pri,
                source=f"producer:cron:{job.name}",
            ),
            producer_id="producer_cron",
        )
        logger.debug("Cron event fired: %s → %s", job.name, job.topic)

    # ── Runtime job management ───────────────────────────────────────

    def add_job(self, job: CronJob) -> bool:
        """Add a job at runtime. Returns True on success."""
        self._jobs.append(job)
        if self._scheduler and self._running:
            self._add_job_to_scheduler(job)
        return True

    def remove_job(self, name: str) -> bool:
        """Remove a job by name. Returns True if found."""
        self._jobs = [j for j in self._jobs if j.name != name]
        if self._scheduler:
            try:
                self._scheduler.remove_job(f"cron_{name}")
            except Exception:
                pass
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return info about all configured jobs."""
        result = []
        for job in self._jobs:
            info: dict[str, Any] = {
                "name": job.name,
                "expression": job.expression,
                "topic": job.topic,
                "payload": job.payload,
                "priority": job.priority,
            }
            # Add next fire time if scheduler is running
            if self._scheduler:
                try:
                    sched_job = self._scheduler.get_job(f"cron_{job.name}")
                    if sched_job and sched_job.next_run_time:
                        info["next_run"] = sched_job.next_run_time.isoformat()
                except Exception:
                    pass
            result.append(info)
        return result
