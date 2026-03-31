"""Abstract base producer."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseProducer(ABC):
    """Base class for all event producers."""

    @abstractmethod
    async def start(self) -> None:
        """Start producing events."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop producing events."""
        ...

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Whether the producer is currently running."""
        ...
