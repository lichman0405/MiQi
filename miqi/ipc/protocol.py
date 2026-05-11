"""Pydantic models for JSON-RPC 2.0 over stdio.

Every message is a single JSON object terminated by a newline.
Request/response/error follow the JSON-RPC 2.0 spec.
Events are server-initiated notifications (no id, no response expected).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── JSON-RPC 2.0 core ──────────────────────────────────────────────────────

class JsonRpcRequest(BaseModel):
    """A JSON-RPC 2.0 request."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None


class JsonRpcResponse(BaseModel):
    """A JSON-RPC 2.0 response (success or error)."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any | None = None
    error: JsonRpcError | None = None


class JsonRpcEvent(BaseModel):
    """A server-initiated event notification.

    Not part of the JSON-RPC 2.0 spec but follows the same envelope.
    Events have no id and expect no response.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


# ── Standard JSON-RPC error codes ───────────────────────────────────────────

ERROR_PARSE_ERROR = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603

# ── App-level error codes (-32001 … -32099) ──────────────────────────────

ERROR_EXECUTION_BUSY = -32001


def make_error_response(
    request_id: int | str | None,
    code: int,
    message: str,
    data: Any | None = None,
) -> JsonRpcResponse:
    """Build a JSON-RPC error response."""
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code, message=message, data=data),
    )


def make_success_response(
    request_id: int | str | None,
    result: Any,
) -> JsonRpcResponse:
    """Build a JSON-RPC success response."""
    return JsonRpcResponse(id=request_id, result=result)
