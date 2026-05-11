"""Typed event models — all payloads are safe for IPC transit.

No API keys, MCP env secrets, or other credentials appear in any payload.
Sensitive command text in ``ApprovalRequested`` is redacted and truncated
rather than passed verbatim.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class _EventBase(BaseModel):
    """Common fields for every structured event."""

    type: str  # discriminator — always matches the event class name


# ── Run lifecycle ──────────────────────────────────────────────────────────

class RunStarted(_EventBase):
    type: Literal["RunStarted"] = "RunStarted"
    execution_id: str
    session_key: str
    channel: str = ""
    preview: str = Field(default="", description="First ~60 chars of the user message")


class RunCompleted(_EventBase):
    type: Literal["RunCompleted"] = "RunCompleted"
    execution_id: str
    session_key: str
    response_preview: str = Field(default="", description="First ~120 chars of assistant response")


class RunCancelled(_EventBase):
    type: Literal["RunCancelled"] = "RunCancelled"
    execution_id: str
    session_key: str
    reason: str = ""


# ── Message ────────────────────────────────────────────────────────────────

class MessageDelta(_EventBase):
    type: Literal["MessageDelta"] = "MessageDelta"
    execution_id: str
    delta: str = Field(default="", description="Incremental text chunk")


class MessageFinal(_EventBase):
    type: Literal["MessageFinal"] = "MessageFinal"
    execution_id: str
    content: str = Field(default="", description="Full assistant response (truncated if huge)")


# ── Tool calls ─────────────────────────────────────────────────────────────

class ToolCallStarted(_EventBase):
    type: Literal["ToolCallStarted"] = "ToolCallStarted"
    execution_id: str
    tool_name: str
    tool_call_id: str = ""


class ToolProgress(_EventBase):
    type: Literal["ToolProgress"] = "ToolProgress"
    execution_id: str
    tool_name: str
    tool_call_id: str = ""
    elapsed_seconds: float = 0.0
    message: str = ""


class ToolResult(_EventBase):
    type: Literal["ToolResult"] = "ToolResult"
    execution_id: str
    tool_name: str
    tool_call_id: str = ""
    preview: str = Field(default="", description="First ~200 chars of tool result")
    is_error: bool = False


# ── Approval ───────────────────────────────────────────────────────────────

class ApprovalRequested(_EventBase):
    type: Literal["ApprovalRequested"] = "ApprovalRequested"
    approval_id: str
    execution_id: str
    tool_name: str
    tool_call_id: str = ""
    pattern_description: str = ""
    command_preview: str = Field(
        default="",
        description="Redacted and truncated command preview — never the full sensitive text",
    )


class ApprovalResolved(_EventBase):
    type: Literal["ApprovalResolved"] = "ApprovalResolved"
    approval_id: str
    execution_id: str
    tool_call_id: str = ""
    decision: Literal["once", "session", "always", "deny"] = "deny"


# ── Session / workspace / memory / MCP / cron ──────────────────────────────

class SessionChanged(_EventBase):
    type: Literal["SessionChanged"] = "SessionChanged"
    session_key: str
    action: Literal["created", "updated", "deleted", "renamed"] = "updated"


class MemoryChanged(_EventBase):
    type: Literal["MemoryChanged"] = "MemoryChanged"
    action: Literal["snapshot", "lesson", "flush", "reset"] = "flush"


class WorkspaceIndexChanged(_EventBase):
    type: Literal["WorkspaceIndexChanged"] = "WorkspaceIndexChanged"
    path: str = ""
    action: Literal["created", "modified", "deleted"] = "modified"


class McpStatusChanged(_EventBase):
    type: Literal["McpStatusChanged"] = "McpStatusChanged"
    server_name: str
    status: Literal["connecting", "connected", "disconnected", "error"] = "disconnected"


class CronJobChanged(_EventBase):
    type: Literal["CronJobChanged"] = "CronJobChanged"
    job_id: str = ""
    job_name: str = ""
    action: Literal["added", "updated", "deleted", "executed"] = "updated"


class Error(_EventBase):
    type: Literal["Error"] = "Error"
    execution_id: str = ""
    message: str = ""
    source: str = ""


class QueueUpdated(_EventBase):
    type: Literal["QueueUpdated"] = "QueueUpdated"
    queue_size: int = 0
    active_execution_id: str = ""
    pending_execution_ids: list[str] = Field(default_factory=list)
    status: Literal["idle", "queued", "running", "waiting_for_approval", "cancelling"] = "idle"


# ── Union type for type checkers ───────────────────────────────────────────

RuntimeEvent = (
    RunStarted
    | RunCompleted
    | RunCancelled
    | MessageDelta
    | MessageFinal
    | ToolCallStarted
    | ToolProgress
    | ToolResult
    | ApprovalRequested
    | ApprovalResolved
    | SessionChanged
    | MemoryChanged
    | WorkspaceIndexChanged
    | McpStatusChanged
    | CronJobChanged
    | Error
    | QueueUpdated
)

EVENT_TYPES: dict[str, type[_EventBase]] = {
    "RunStarted": RunStarted,
    "RunCompleted": RunCompleted,
    "RunCancelled": RunCancelled,
    "MessageDelta": MessageDelta,
    "MessageFinal": MessageFinal,
    "ToolCallStarted": ToolCallStarted,
    "ToolProgress": ToolProgress,
    "ToolResult": ToolResult,
    "ApprovalRequested": ApprovalRequested,
    "ApprovalResolved": ApprovalResolved,
    "SessionChanged": SessionChanged,
    "MemoryChanged": MemoryChanged,
    "WorkspaceIndexChanged": WorkspaceIndexChanged,
    "McpStatusChanged": McpStatusChanged,
    "CronJobChanged": CronJobChanged,
    "Error": Error,
    "QueueUpdated": QueueUpdated,
}
