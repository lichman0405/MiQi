"""EventEmitter — async pub/sub for structured runtime events.

Independent of ``MessageBus``.  Desktop-backend and future consumers
subscribe here; CLI/gateway continue to use the existing outbound queue
untouched.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable

from loguru import logger

from miqi.events.models import _EventBase, RuntimeEvent


# Type alias for subscriber callbacks
Subscriber = Callable[[RuntimeEvent], Awaitable[None] | None]


class EventEmitter:
    """Async event emitter with sequential fan-out to subscribers.

    Designed to be wired into ``Runtime`` so that any component can
    ``emit(event)`` and all registered subscribers receive it.

    Subscribers are called sequentially in registration order.  A
    failing subscriber (one that raises) does not prevent subsequent
    subscribers from receiving the event — the error is logged and the
    loop continues.

    A *slow* subscriber, however, will delay all later subscribers
    because fan-out is sequential, not concurrent.  If that becomes a
    problem, switch to concurrent dispatch (``asyncio.gather``) and
    accept the ordering trade-off.

    Thread-safety: intended for single-event-loop use (like the rest of
    MiQi's async runtime).  Subscribers must not block the loop.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> None:
        """Register a callback.  Callbacks are called in order."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        """Remove a previously registered callback."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def emit(self, event: RuntimeEvent) -> None:
        """Emit an event to all subscribers.

        Fan-out is sequential: subscribers are called in registration order.
        A failing subscriber (one that raises) does not prevent subsequent
        subscribers from receiving the event — the error is logged and the
        loop continues.  A *slow* subscriber, however, will delay all later
        subscribers because fan-out is sequential, not concurrent.
        """
        for cb in self._subscribers:
            try:
                result = cb(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("EventEmitter subscriber error: {}", exc)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
