"""Abstract base consumer."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseConsumer(ABC):
    """Base class for all event consumers."""

    @abstractmethod
    async def start(self) -> None:
        """Start the consumer."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the consumer."""
        ...

    @abstractmethod
    async def notify(self) -> None:
        """Notify the consumer that new work is available."""
        ...
