"""Lifecycle manager — expiry sweeper, retry scheduler.

Runs background tasks:
- Expire events past their expires_at
- Retry failed assignments with backoff
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiventbus.storage.db import Database

logger = logging.getLogger(__name__)

RETRY_BACKOFFS = [5, 15, 45]  # seconds


class LifecycleManager:
    """Manages event expiry and assignment retries."""

    def __init__(self, db: Database):
        self.db = db
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._running = True
        self._tasks.append(asyncio.create_task(self._expiry_loop()))
        self._tasks.append(asyncio.create_task(self._retry_loop()))
        logger.info("Lifecycle manager started")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _expiry_loop(self) -> None:
        """Sweep expired events every 30 seconds."""
        while self._running:
            try:
                now = datetime.now(timezone.utc).isoformat()
                cursor = await self.db.conn.execute(
                    """UPDATE events SET status = 'expired'
                       WHERE expires_at IS NOT NULL AND expires_at < ?
                       AND status NOT IN ('expired', 'completed', 'failed')""",
                    (now,),
                )
                if cursor.rowcount > 0:
                    await self.db.conn.commit()
                    logger.info("Expired %d events", cursor.rowcount)
            except Exception as e:
                logger.error("Expiry sweep error: %s", e)
            await asyncio.sleep(30)

    async def _retry_loop(self) -> None:
        """Check for failed assignments eligible for retry every 10 seconds."""
        while self._running:
            try:
                # Find failed assignments with retry budget
                cursor = await self.db.conn.execute(
                    """SELECT ea.id, ea.event_id, ea.agent_id, ea.retry_count, e.max_retries
                       FROM event_assignments ea
                       JOIN events e ON ea.event_id = e.id
                       WHERE ea.status = 'failed'
                       AND ea.retry_count < COALESCE(e.max_retries, 0)
                       LIMIT 10""",
                )
                rows = await cursor.fetchall()
                for row in rows:
                    new_count = row["retry_count"] + 1
                    await self.db.conn.execute(
                        "UPDATE event_assignments SET status = 'pending', retry_count = ? WHERE id = ?",
                        (new_count, row["id"]),
                    )
                    logger.info(
                        "Retrying assignment %s (attempt %d/%d)",
                        row["id"], new_count, row["max_retries"],
                    )
                if rows:
                    await self.db.conn.commit()
            except Exception as e:
                logger.error("Retry sweep error: %s", e)
            await asyncio.sleep(10)
