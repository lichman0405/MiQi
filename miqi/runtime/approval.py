"""ToolApprovalService — async approval flow for desktop IPC.

When a tool (currently ExecTool) detects a dangerous command, the agent loop
pauses execution and emits an ``ApprovalRequested`` event.  The desktop
frontend shows an approval card, the user clicks Approve/Deny, and the
IPC handler resolves the pending ``asyncio.Future`` so the agent loop
resumes.

CLI and gateway channels bypass this service entirely — they continue using
the synchronous ``command_approval.prompt_dangerous_approval`` path.

Single source of truth for session/permanent approvals:
``miqi.agent.command_approval``, which ExecTool already queries via
``is_approved()``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loguru import logger

from miqi.events.emitter import EventEmitter
from miqi.events.models import ApprovalRequested, ApprovalResolved


class ApprovalDecision(str, Enum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    DENY = "deny"


# ── Secret redaction for command previews ──────────────────────────────────

_REDACTED = "********"

# Two-stage redaction:
# 1. Context-aware: any value after password/secret/api_key/token assignments
# 2. Bare patterns: sk-xxx, eyJ... JWT tokens, long base64 blobs
_CONTEXT_SECRET = re.compile(
    r"""
    (?P<prefix>
        (?:api[_-]?key|token|secret|password|passwd|authorization)
        [\s=:"']*
        (?:Bearer\s+)?
    )
    (?P<value>
        \S+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BARE_SECRET = re.compile(
    r"""
    (?P<bare>
        sk-[\w-]+
        | ey[\w-]{10,}
        | [A-Za-z0-9+/]{40,}={0,2}
    )
    """,
    re.VERBOSE,
)


def redact_command_preview(command: str, max_len: int = 80) -> str:
    """Truncate and redact secrets from a command for safe IPC transit."""
    redacted = _CONTEXT_SECRET.sub(r"\g<prefix>" + _REDACTED, command)
    redacted = _BARE_SECRET.sub(_REDACTED, redacted)
    if len(redacted) > max_len:
        return redacted[:max_len] + "..."
    return redacted


@dataclass
class PendingApproval:
    """An approval request awaiting a user decision."""

    approval_id: str
    execution_id: str
    tool_name: str
    tool_call_id: str
    session_key: str
    pattern_description: str
    command_preview: str
    future: asyncio.Future[ApprovalDecision] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class ToolApprovalService:
    """Manage pending approval requests and their resolution.

    Intended for single-event-loop use (same constraint as ExecutionManager).
    Session and permanent approvals are recorded in
    ``miqi.agent.command_approval`` which ExecTool checks via
    ``is_approved()`` — this is the single source of truth, not an
    internal set.
    """

    def __init__(self, events: EventEmitter, timeout: float = 120.0) -> None:
        self._events = events
        self._timeout = timeout
        self._pending: dict[str, PendingApproval] = {}

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def get_pending(self, approval_id: str) -> PendingApproval | None:
        return self._pending.get(approval_id)

    def list_pending(self) -> list[PendingApproval]:
        return list(self._pending.values())

    async def request_approval(
        self,
        execution_id: str,
        tool_name: str,
        tool_call_id: str,
        session_key: str,
        pattern_description: str,
        command: str,
    ) -> ApprovalDecision:
        """Request approval for a dangerous tool invocation.

        Emits ``ApprovalRequested`` (with ``approval_id``) and waits for
        resolution (via ``resolve``) or timeout (defaults to deny).

        Returns the decision.
        """
        approval_id = uuid.uuid4().hex[:12]
        command_preview = redact_command_preview(command)

        pending = PendingApproval(
            approval_id=approval_id,
            execution_id=execution_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            session_key=session_key,
            pattern_description=pattern_description,
            command_preview=command_preview,
        )
        self._pending[approval_id] = pending

        await self._events.emit(ApprovalRequested(
            approval_id=approval_id,
            execution_id=execution_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            pattern_description=pattern_description,
            command_preview=command_preview,
        ))

        # Wait for resolution or timeout
        try:
            decision = await asyncio.wait_for(pending.future, timeout=self._timeout)
        except asyncio.TimeoutError:
            decision = ApprovalDecision.DENY
            logger.warning("Approval {} timed out — denying", approval_id)
            await self._resolve_internal(approval_id, decision)
        except asyncio.CancelledError:
            # Execution was cancelled while waiting for approval
            decision = ApprovalDecision.DENY
            self._cleanup_pending(approval_id)
            raise

        return decision

    async def resolve(self, approval_id: str, decision: ApprovalDecision) -> bool:
        """Resolve a pending approval request.

        Returns True if the approval was found and resolved, False otherwise.
        """
        pending = self._pending.get(approval_id)
        if pending is None:
            logger.warning("resolve: approval {} not found", approval_id)
            return False

        if pending.future.done():
            logger.warning("resolve: approval {} already resolved", approval_id)
            return False

        await self._resolve_internal(approval_id, decision)
        return True

    def cancel_all(self) -> None:
        """Cancel all pending approvals (e.g. on execution cancellation)."""
        for approval_id in list(self._pending):
            self._cleanup_pending(approval_id)

    async def _resolve_internal(self, approval_id: str, decision: ApprovalDecision) -> None:
        """Set the future result, record approvals, emit ApprovalResolved, cleanup."""
        pending = self._pending.get(approval_id)
        if pending is None:
            return

        if not pending.future.done():
            pending.future.set_result(decision)

        # Record approvals in command_approval (single source of truth).
        # ExecTool._check_approval() queries command_approval.is_approved()
        # so subsequent matches in the same session auto-pass.
        if decision == ApprovalDecision.SESSION and pending.session_key and pending.pattern_description:
            _apply_session_approval(pending.session_key, pending.pattern_description)
        elif decision == ApprovalDecision.ALWAYS and pending.session_key and pending.pattern_description:
            _apply_session_approval(pending.session_key, pending.pattern_description)
            self._persist_permanent(pending.pattern_description)

        self._cleanup_pending(approval_id)

        await self._events.emit(ApprovalResolved(
            approval_id=approval_id,
            execution_id=pending.execution_id,
            tool_call_id=pending.tool_call_id,
            decision=decision.value,
        ))

    def _cleanup_pending(self, approval_id: str) -> None:
        """Remove from pending dict. Best-effort cancel the future if unresolved."""
        pending = self._pending.pop(approval_id, None)
        if pending and not pending.future.done():
            pending.future.cancel()

    @staticmethod
    def _persist_permanent(pattern_description: str) -> None:
        """Persist an 'always' approval to config (best-effort)."""
        try:
            from miqi.agent.command_approval import approve_permanent, _save_permanent_allowlist
            approve_permanent(pattern_description)
            _save_permanent_allowlist()
        except Exception as exc:
            logger.warning("Could not persist permanent approval: {}", exc)


def _apply_session_approval(session_key: str, pattern_key: str) -> None:
    """Record a session-level approval in command_approval."""
    from miqi.agent.command_approval import approve_session
    approve_session(session_key, pattern_key)
