"""ExecutionManager — tracks execution lifecycle and enables cancellation.

Every run submitted through the desktop backend (or any future IPC consumer)
gets a unique execution_id.  ExecutionManager records the active task,
manages a small pending queue, and supports cooperative cancellation by
cancelling the underlying ``asyncio.Task``.

The manager is intentionally thin: it does NOT execute the agent loop itself
—that responsibility stays with ``AgentService``.  It only tracks state and
emits structured events.
"""

from __future__ import annotations

import asyncio
import uuid
from enum import Enum
from typing import Optional

from loguru import logger

from miqi.events.emitter import EventEmitter
from miqi.events.models import (
    QueueUpdated,
    RunCancelled,
    RunCompleted,
    RunStarted,
)


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ExecutionRecord:
    __slots__ = ("execution_id", "session_key", "channel", "preview", "task", "status")

    def __init__(
        self,
        execution_id: str,
        session_key: str,
        channel: str = "",
        preview: str = "",
        task: asyncio.Task | None = None,
        status: ExecutionStatus = ExecutionStatus.QUEUED,
    ) -> None:
        self.execution_id = execution_id
        self.session_key = session_key
        self.channel = channel
        self.preview = preview
        self.task = task
        self.status = status


def generate_execution_id() -> str:
    return uuid.uuid4().hex[:12]


class ExecutionManager:
    """Track active and pending executions, support cancellation.

    Not thread-safe — intended for single-event-loop use.

    Phase 4.1 strategy: single active execution.  If an active execution
    already exists, ``start`` returns False instead of registering.
    """

    def __init__(self, events: EventEmitter) -> None:
        self._events = events
        self._executions: dict[str, ExecutionRecord] = {}
        self._active_id: str | None = None
        self._pending_ids: list[str] = []

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def active_execution_id(self) -> str | None:
        return self._active_id

    @property
    def pending_execution_ids(self) -> list[str]:
        return list(self._pending_ids)

    @property
    def queue_size(self) -> int:
        size = len(self._pending_ids)
        if self._active_id is not None:
            size += 1
        return size

    @property
    def has_active(self) -> bool:
        return self._active_id is not None

    def get(self, execution_id: str) -> ExecutionRecord | None:
        return self._executions.get(execution_id)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(
        self,
        execution_id: str,
        session_key: str,
        channel: str = "",
        preview: str = "",
        task: asyncio.Task | None = None,
    ) -> bool:
        """Register a new execution as running and emit RunStarted.

        Returns False if an active execution already exists (single-active
        strategy).  The caller must NOT create a task if this returns False.

        Emits RunStarted and QueueUpdated only when registration succeeds.
        """
        if self._active_id is not None:
            return False

        rec = ExecutionRecord(
            execution_id=execution_id,
            session_key=session_key,
            channel=channel,
            preview=preview[:60],
            task=task,
            status=ExecutionStatus.RUNNING,
        )
        self._executions[execution_id] = rec
        self._active_id = execution_id

        await self._events.emit(RunStarted(
            execution_id=execution_id,
            session_key=session_key,
            channel=channel,
            preview=rec.preview,
        ))
        await self._emit_queue()
        return True

    async def complete(self, execution_id: str, response_preview: str = "") -> None:
        """Mark execution as completed and emit RunCompleted."""
        rec = self._executions.pop(execution_id, None)
        if rec is None:
            return
        rec.status = ExecutionStatus.COMPLETED
        if self._active_id == execution_id:
            self._active_id = None
        await self._events.emit(RunCompleted(
            execution_id=execution_id,
            session_key=rec.session_key,
            response_preview=response_preview[:120],
        ))
        await self._emit_queue()

    async def fail(self, execution_id: str, error_message: str = "") -> None:
        """Mark execution as failed.  Emits RunCompleted with error info."""
        rec = self._executions.pop(execution_id, None)
        if rec is None:
            return
        rec.status = ExecutionStatus.FAILED
        if self._active_id == execution_id:
            self._active_id = None
        await self._events.emit(RunCompleted(
            execution_id=execution_id,
            session_key=rec.session_key,
            response_preview=f"Error: {error_message}"[:120] if error_message else "",
        ))
        await self._emit_queue()

    async def cancel(self, execution_id: str, reason: str = "user") -> bool:
        """Request cancellation of an execution.

        Sets status to CANCELLING and cancels the underlying asyncio.Task.
        The record is kept (so ``should_cancel`` returns True) until
        ``mark_cancelled`` is called after the task finishes cleanup.

        Emits RunCancelled and QueueUpdated(status="cancelling").

        Returns True if the execution was found, False otherwise.
        Repeated calls for an already-cancelling execution return True
        without re-emitting events.
        """
        rec = self._executions.get(execution_id)
        if rec is None:
            logger.warning("cancel: execution {} not found", execution_id)
            return False

        if rec.status == ExecutionStatus.CANCELLING:
            return True  # already cancelling — no duplicate event

        rec.status = ExecutionStatus.CANCELLING
        if rec.task and not rec.task.done():
            rec.task.cancel()

        await self._events.emit(RunCancelled(
            execution_id=execution_id,
            session_key=rec.session_key,
            reason=reason,
        ))
        await self._emit_queue()
        return True

    async def mark_cancelled(self, execution_id: str) -> None:
        """Remove a cancelling execution after its task has finished cleanup.

        Called by AgentService once the CancelledError has been handled.
        Emits QueueUpdated(idle) and clears the active slot.
        """
        rec = self._remove_cancelled_record(execution_id)
        if rec is None:
            return
        await self._emit_queue()

    async def finalize_cancelled(self, execution_id: str) -> None:
        """Finalize a cancelled execution from within a CancelledError handler.

        Performs synchronous cleanup first (remove record, clear active)
        so queue state is consistent even if the async event emit is
        re-cancelled.  Then best-effort emits QueueUpdated(idle).

        This is the method callers should use inside ``except
        CancelledError`` blocks where further ``await`` may raise
        ``CancelledError`` again.
        """
        self._remove_cancelled_record(execution_id)
        try:
            await self._emit_queue()
        except asyncio.CancelledError:
            pass

    def _remove_cancelled_record(self, execution_id: str) -> ExecutionRecord | None:
        """Remove a cancelling record and clear the active slot."""
        rec = self._executions.pop(execution_id, None)
        if rec is None:
            return None
        rec.status = ExecutionStatus.CANCELLED
        if self._active_id == execution_id:
            self._active_id = None
        return rec

    def should_cancel(self, execution_id: str) -> bool:
        """Check if an execution has been marked for cancellation."""
        rec = self._executions.get(execution_id)
        return rec is not None and rec.status == ExecutionStatus.CANCELLING

    def attach_task(self, execution_id: str, task: asyncio.Task) -> None:
        """Attach an asyncio.Task to an existing execution record."""
        rec = self._executions.get(execution_id)
        if rec is not None:
            rec.task = task

    # ── Queue helpers ───────────────────────────────────────────────────────

    def _queue_status(self) -> str:
        if self._active_id is not None:
            rec = self._executions.get(self._active_id)
            if rec and rec.status == ExecutionStatus.CANCELLING:
                return "cancelling"
            return "running"
        if self._pending_ids:
            return "queued"
        return "idle"

    async def _emit_queue(self) -> None:
        await self._events.emit(QueueUpdated(
            queue_size=self.queue_size,
            active_execution_id=self._active_id or "",
            pending_execution_ids=self.pending_execution_ids,
            status=self._queue_status(),  # type: ignore[arg-type]
        ))
