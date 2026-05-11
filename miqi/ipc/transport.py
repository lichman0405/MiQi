"""Stdio transport — newline-delimited JSON-RPC over stdin/stdout.

Reads JSON-RPC requests from stdin, dispatches them, and writes
responses/events to stdout.  stderr is reserved for loguru logging.

This module is designed to be driven by an async event loop created
in the ``desktop-backend`` CLI command.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING

from loguru import logger

from miqi.events.models import RuntimeEvent
from miqi.events.emitter import EventEmitter
from miqi.ipc.protocol import (
    ERROR_INVALID_REQUEST,
    ERROR_PARSE_ERROR,
    JsonRpcEvent,
    JsonRpcRequest,
    JsonRpcResponse,
    make_error_response,
)

if TYPE_CHECKING:
    from miqi.ipc.handlers import RpcDispatcher


async def read_requests(
    dispatcher: "RpcDispatcher",
    reader: asyncio.StreamReader | None = None,
    event_emitter: EventEmitter | None = None,
) -> None:
    """Read newline-delimited JSON-RPC from *reader* and dispatch each line.

    By default uses stdin (opened in binary mode).  Accepts a custom
    ``reader`` for testing.

    If *event_emitter* is provided, subscribes to it and writes all
    emitted runtime events as ``JsonRpcEvent`` notifications to stdout.
    """
    if event_emitter is not None:
        event_emitter.subscribe(_event_to_stdout)

    try:
        while True:
            if reader is None:
                # Windows' default asyncio event loop does not reliably support
                # connect_read_pipe for stdio pipes.  A background blocking
                # readline keeps the transport portable while preserving the
                # newline-delimited stdio contract.
                line: bytes = await asyncio.to_thread(sys.stdin.buffer.readline)
            else:
                line = await reader.readline()
            if not line:
                logger.debug("IPC transport: EOF on stdin, shutting down")
                break

            text = line.decode("utf-8").strip()
            if not text:
                continue

            response = await _handle_line(text, dispatcher)
            if response is not None:
                _write_response(response)
    finally:
        if event_emitter is not None:
            event_emitter.unsubscribe(_event_to_stdout)


async def _event_to_stdout(event: RuntimeEvent) -> None:
    """Write runtime events as method-style JSON-RPC notifications."""
    envelope = JsonRpcEvent(
        method=event.type,
        params=event.model_dump(exclude={"type"}),
    )
    payload = envelope.model_dump(exclude_none=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


async def _handle_line(
    text: str,
    dispatcher: "RpcDispatcher",
) -> JsonRpcResponse | None:
    """Parse one line of JSON and dispatch.  Returns a response or None."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return make_error_response(
            None, ERROR_PARSE_ERROR,
            f"Parse error: {exc}",
        )

    is_notification = isinstance(raw, dict) and "id" not in raw

    try:
        request = JsonRpcRequest.model_validate(raw)
    except Exception as exc:
        return make_error_response(
            raw.get("id") if isinstance(raw, dict) else None,
            ERROR_INVALID_REQUEST,
            f"Invalid request: {exc}",
        )

    if is_notification:
        # Notification — no response expected, but still dispatch for side effects.
        logger.debug("IPC notification: {}", request.method)
        await dispatcher.dispatch(request)
        return None

    return await dispatcher.dispatch(request)


def _write_response(response: JsonRpcResponse) -> None:
    """Serialize and write a response to stdout."""
    payload = response.model_dump(exclude_none=True)
    payload["id"] = response.id
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
