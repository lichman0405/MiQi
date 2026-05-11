"""AgentService — thin orchestration layer over AgentLoop.

Wraps ``agent.process_direct`` with execution tracking, structured events,
and cancellation support.  The IPC layer (``chat.send``) calls
``AgentService.send`` instead of invoking ``process_direct`` directly.

CLI ``miqi agent`` and ``miqi gateway`` keep their existing code paths
untouched — they do not go through AgentService.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Awaitable

from loguru import logger

from miqi.events.models import Error, MessageDelta, MessageFinal
from miqi.runtime.execution import ExecutionManager, generate_execution_id

if TYPE_CHECKING:
    from miqi.agent.loop import AgentLoop
    from miqi.events.emitter import EventEmitter
    from miqi.runtime.approval import ToolApprovalService


class AgentBusyError(Exception):
    """Raised when a send is attempted while an execution is already active."""


class AgentService:
    """Orchestrates agent execution with execution_id, events, and cancellation.

    Phase 4.1 strategy: single active execution.  If an execution is
    already running, ``send`` raises ``AgentBusyError``.
    """

    def __init__(
        self,
        agent: AgentLoop,
        events: EventEmitter,
        approval_service: ToolApprovalService | None = None,
    ) -> None:
        self._agent = agent
        self._events = events
        self._manager = ExecutionManager(events)
        self._approval_service = approval_service
        if approval_service is not None:
            self._wire_approval(approval_service)

    @property
    def manager(self) -> ExecutionManager:
        return self._manager

    @property
    def approval_service(self) -> ToolApprovalService | None:
        return self._approval_service

    def _wire_approval(self, svc: ToolApprovalService) -> None:
        """Wire the approval callback into ExecTool on the agent's registry."""
        from miqi.agent.tools.shell import ExecTool

        tools = getattr(self._agent, "tools", None)
        if tools is None:
            return
        exec_tool = tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.approval_fn = self._make_approval_fn(svc)

    def _make_approval_fn(
        self, svc: ToolApprovalService,
    ) -> Callable[[str, str, str, str, str], Awaitable[str]]:
        """Create the approval callback that ExecTool will call.

        Returns an async callable with signature:
            (command, pattern_description, session_key, execution_id, tool_call_id) -> decision str
        """
        async def _approval_fn(
            command: str,
            pattern_description: str,
            session_key: str,
            execution_id: str,
            tool_call_id: str,
        ) -> str:
            from miqi.runtime.approval import ApprovalDecision
            decision = await svc.request_approval(
                execution_id=execution_id,
                tool_name="exec",
                tool_call_id=tool_call_id,
                session_key=session_key,
                pattern_description=pattern_description,
                command=command,
            )
            return decision.value

        return _approval_fn

    def _make_content_delta_fn(
        self, execution_id: str,
    ) -> Callable[[str], Awaitable[None]] | None:
        """Create streaming delta callback that emits MessageDelta events.

        Returns None when the event emitter has no subscribers (CLI/gateway
        path), so there is zero streaming overhead for non-desktop paths.
        """
        if self._events.subscriber_count == 0:
            return None

        async def _on_delta(text: str) -> None:
            try:
                await self._events.emit(MessageDelta(
                    execution_id=execution_id,
                    delta=text,
                ))
            except Exception:
                pass  # best-effort

        return _on_delta

    async def send(
        self,
        message: str,
        *,
        session_key: str = "desktop:default",
        channel: str = "desktop",
        chat_id: str = "default",
    ) -> dict:
        """Submit a message for execution and return execution info immediately.

        The actual work runs in a spawned ``asyncio.Task`` so the caller
        (IPC handler) can return the execution_id without waiting for
        completion.

        Raises ``AgentBusyError`` if an execution is already active.

        Returns ``{"execution_id": str}`` on success.
        """
        execution_id = generate_execution_id()
        preview = message[:60]

        # Register FIRST — if manager rejects (active exists), we never
        # create a background task.
        registered = await self._manager.start(
            execution_id=execution_id,
            session_key=session_key,
            channel=channel,
            preview=preview,
        )
        if not registered:
            raise AgentBusyError(
                f"Cannot start execution {execution_id}: "
                f"another execution is already active"
            )

        # Now create the task and attach it to the record.
        task = asyncio.create_task(
            self._run(execution_id, message, session_key, channel, chat_id),
        )
        self._manager.attach_task(execution_id, task)

        return {"execution_id": execution_id}

    async def cancel(self, execution_id: str, reason: str = "user") -> bool:
        """Request cancellation of an execution.

        Returns True if the execution was found, False otherwise.
        """
        return await self._manager.cancel(execution_id, reason=reason)

    # ── Internal ────────────────────────────────────────────────────────────

    async def _run(
        self,
        execution_id: str,
        message: str,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> str:
        """Run the agent and handle completion / cancellation / errors."""
        # Expose execution_id to the agent loop so tool approval can
        # reference it when emitting ApprovalRequested events.
        self._agent._current_execution_id = execution_id

        # Build streaming callback (only when events are available —
        # desktop path).  CLI/gateway have no event emitter subscriber
        # and the callback remains None, so no streaming overhead.
        on_content_delta = self._make_content_delta_fn(execution_id)

        try:
            response = await self._agent.process_direct(
                message,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                on_content_delta=on_content_delta,
            )
        except asyncio.CancelledError:
            # Task was cancelled.  Synchronous cleanup runs first (remove
            # record, clear active slot), then best-effort emit of
            # QueueUpdated(idle).  If the emit is re-cancelled, the queue
            # state is already consistent from the sync cleanup.
            if self._approval_service:
                self._approval_service.cancel_all()
            await self._manager.finalize_cancelled(execution_id)
            self._agent._current_execution_id = ""
            raise
        except Exception as exc:
            logger.error("AgentService run error ({}): {}", execution_id, exc)
            await self._events.emit(Error(
                execution_id=execution_id,
                message=str(exc),
                source="agent",
            ))
            await self._manager.fail(execution_id, error_message=str(exc))
            self._agent._current_execution_id = ""
            return ""
        else:
            # Emit MessageFinal with the complete response when there are
            # subscribers (desktop path).  CLI/gateway have no subscribers.
            if self._events.subscriber_count > 0:
                try:
                    await self._events.emit(MessageFinal(
                        execution_id=execution_id,
                        content=response[:8192] if response else "",
                    ))
                except Exception:
                    pass  # best-effort
            # Only complete if the manager still knows about this execution
            # and it hasn't been cancelled.
            rec = self._manager.get(execution_id)
            if rec is not None:
                preview = response[:120] if response else ""
                await self._manager.complete(execution_id, response_preview=preview)
            self._agent._current_execution_id = ""
            return response
