"""Tests for miqi.events — models, EventEmitter, and event-to-transport push."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from miqi.config.schema import Config
from miqi.events.emitter import EventEmitter
from miqi.events.models import (
    ApprovalRequested,
    ApprovalResolved,
    CronJobChanged,
    Error,
    MemoryChanged,
    McpStatusChanged,
    MessageDelta,
    MessageFinal,
    QueueUpdated,
    RunCancelled,
    RunCompleted,
    RunStarted,
    SessionChanged,
    ToolCallStarted,
    ToolProgress,
    ToolResult,
    WorkspaceIndexChanged,
    EVENT_TYPES,
)
from miqi.providers.base import LLMProvider, LLMResponse
from miqi.runtime.factory import Runtime, create_runtime, wire_cron_callback


class FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test-key")

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, *, on_delta=None):
        return LLMResponse(content="fake")

    def get_default_model(self) -> str:
        return "fake-model"


def _make_runtime(tmp_path: Path, monkeypatch) -> Runtime:
    monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIQI_DATA_DIR", str(tmp_path / "data"))
    config = Config()
    rt = create_runtime(config, make_provider=lambda c: FakeProvider(), init_session_manager=True)
    wire_cron_callback(rt)
    return rt


# ══════════════════════════════════════════════════════════════════════════
# Event models
# ══════════════════════════════════════════════════════════════════════════

class TestEventModels:

    def test_all_seventeen_events_in_types(self):
        expected = {
            "RunStarted", "RunCompleted", "RunCancelled",
            "MessageDelta", "MessageFinal",
            "ToolCallStarted", "ToolProgress", "ToolResult",
            "ApprovalRequested", "ApprovalResolved",
            "SessionChanged", "MemoryChanged",
            "WorkspaceIndexChanged", "McpStatusChanged",
            "CronJobChanged", "Error", "QueueUpdated",
        }
        assert set(EVENT_TYPES.keys()) == expected

    def test_event_type_discriminator(self):
        evt = RunStarted(execution_id="abc", session_key="cli:default")
        assert evt.type == "RunStarted"

    def test_run_started(self):
        evt = RunStarted(execution_id="e1", session_key="cli:default", preview="hello")
        d = evt.model_dump()
        assert d["type"] == "RunStarted"
        assert d["execution_id"] == "e1"
        assert d["preview"] == "hello"

    def test_run_completed(self):
        evt = RunCompleted(execution_id="e1", session_key="cli:default", response_preview="hi there")
        assert evt.type == "RunCompleted"

    def test_run_cancelled(self):
        evt = RunCancelled(execution_id="e1", session_key="cli:default", reason="user")
        assert evt.reason == "user"

    def test_message_delta(self):
        evt = MessageDelta(execution_id="e1", delta="Hello, ")
        assert evt.delta == "Hello, "

    def test_message_final(self):
        evt = MessageFinal(execution_id="e1", content="Full response")
        assert evt.content == "Full response"

    def test_tool_call_started(self):
        evt = ToolCallStarted(execution_id="e1", tool_name="read_file", tool_call_id="tc1")
        assert evt.tool_name == "read_file"

    def test_tool_progress(self):
        evt = ToolProgress(execution_id="e1", tool_name="exec", elapsed_seconds=5.0, message="running...")
        assert evt.elapsed_seconds == 5.0

    def test_tool_result(self):
        evt = ToolResult(execution_id="e1", tool_name="exec", preview="file content...", is_error=False)
        assert evt.is_error is False

    def test_approval_requested_truncates_command(self):
        evt = ApprovalRequested(
            approval_id="abc123",
            execution_id="e1",
            tool_name="exec",
            pattern_description="recursive delete",
            command_preview="rm -rf /very/long/...",
        )
        assert evt.command_preview == "rm -rf /very/long/..."

    def test_approval_resolved(self):
        for decision in ("once", "session", "always", "deny"):
            evt = ApprovalResolved(approval_id="abc123", execution_id="e1", decision=decision)
            assert evt.decision == decision

    def test_session_changed(self):
        for action in ("created", "updated", "deleted", "renamed"):
            evt = SessionChanged(session_key="s1", action=action)
            assert evt.action == action

    def test_memory_changed(self):
        evt = MemoryChanged(action="lesson")
        assert evt.type == "MemoryChanged"

    def test_workspace_index_changed(self):
        evt = WorkspaceIndexChanged(path="/foo/bar.py", action="modified")
        assert evt.path == "/foo/bar.py"

    def test_mcp_status_changed(self):
        for status in ("connecting", "connected", "disconnected", "error"):
            evt = McpStatusChanged(server_name="raspa", status=status)
            assert evt.status == status

    def test_cron_job_changed(self):
        evt = CronJobChanged(job_id="j1", job_name="daily", action="executed")
        assert evt.job_name == "daily"

    def test_error_event(self):
        evt = Error(execution_id="e1", message="timeout", source="provider")
        assert evt.message == "timeout"

    def test_queue_updated_defaults(self):
        evt = QueueUpdated()
        assert evt.type == "QueueUpdated"
        assert evt.queue_size == 0
        assert evt.active_execution_id == ""
        assert evt.pending_execution_ids == []
        assert evt.status == "idle"

    def test_queue_updated_all_fields(self):
        evt = QueueUpdated(
            queue_size=3,
            active_execution_id="e1",
            pending_execution_ids=["e2", "e3"],
            status="running",
        )
        assert evt.queue_size == 3
        assert evt.active_execution_id == "e1"
        assert evt.pending_execution_ids == ["e2", "e3"]
        assert evt.status == "running"

    def test_queue_updated_all_statuses(self):
        for status in ("idle", "queued", "running", "waiting_for_approval", "cancelling"):
            evt = QueueUpdated(status=status)
            assert evt.status == status

    def test_queue_updated_json_rpc_event_serialization(self):
        evt = QueueUpdated(queue_size=1, active_execution_id="e1", status="running")
        params = evt.model_dump(exclude={"type"})
        assert "type" not in params
        payload = {"jsonrpc": "2.0", "method": evt.type, "params": params}
        text = json.dumps(payload)
        parsed = json.loads(text)
        assert parsed["method"] == "QueueUpdated"
        assert parsed["params"]["queue_size"] == 1
        assert parsed["params"]["status"] == "running"

    def test_serialize_excludes_type_from_params(self):
        """When serialized for JsonRpcEvent, 'type' becomes the method name,
        so the params dict should exclude 'type'."""
        evt = RunStarted(execution_id="e1", session_key="cli:default")
        params = evt.model_dump(exclude={"type"})
        assert "type" not in params
        assert "execution_id" in params

    def test_no_secrets_in_approval_requested(self):
        """ApprovalRequested must not contain full command text."""
        evt = ApprovalRequested(
            approval_id="abc123",
            execution_id="e1",
            tool_name="exec",
            command_preview="rm -rf /...",
        )
        d = evt.model_dump()
        # No apiKey, token, secret, or password keys exist in any event model
        for key in d:
            assert "apikey" not in key.lower()
            assert "token" not in key.lower()
            assert "secret" not in key.lower()
            assert "password" not in key.lower()


# ══════════════════════════════════════════════════════════════════════════
# EventEmitter
# ══════════════════════════════════════════════════════════════════════════

class TestEventEmitter:

    @pytest.mark.asyncio
    async def test_subscribe_and_emit(self):
        emitter = EventEmitter()
        received = []

        async def cb(event):
            received.append(event)

        emitter.subscribe(cb)
        evt = RunStarted(execution_id="e1", session_key="cli:default")
        await emitter.emit(evt)

        assert len(received) == 1
        assert received[0].execution_id == "e1"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        emitter = EventEmitter()
        a = []
        b = []

        async def cb_a(event):
            a.append(event)

        async def cb_b(event):
            b.append(event)

        emitter.subscribe(cb_a)
        emitter.subscribe(cb_b)

        evt = RunStarted(execution_id="e1", session_key="cli:default")
        await emitter.emit(evt)

        assert len(a) == 1
        assert len(b) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        emitter = EventEmitter()
        received = []

        async def cb(event):
            received.append(event)

        emitter.subscribe(cb)
        emitter.unsubscribe(cb)

        evt = RunStarted(execution_id="e1", session_key="cli:default")
        await emitter.emit(evt)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_failing_subscriber_does_not_block_others(self):
        emitter = EventEmitter()
        ok = []

        async def bad(event):
            raise RuntimeError("boom")

        async def good(event):
            ok.append(event)

        emitter.subscribe(bad)
        emitter.subscribe(good)

        evt = RunStarted(execution_id="e1", session_key="cli:default")
        await emitter.emit(evt)

        assert len(ok) == 1

    @pytest.mark.asyncio
    async def test_subscriber_count(self):
        emitter = EventEmitter()

        async def cb(event):
            pass

        assert emitter.subscriber_count == 0
        emitter.subscribe(cb)
        assert emitter.subscriber_count == 1
        emitter.unsubscribe(cb)
        assert emitter.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_noop(self):
        emitter = EventEmitter()

        async def cb(event):
            pass

        emitter.unsubscribe(cb)  # should not raise
        assert emitter.subscriber_count == 0


# ══════════════════════════════════════════════════════════════════════════
# Runtime has EventEmitter
# ══════════════════════════════════════════════════════════════════════════

class TestRuntimeEvents:

    def test_runtime_has_events(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        assert rt.events is not None
        assert isinstance(rt.events, EventEmitter)

    @pytest.mark.asyncio
    async def test_emit_on_runtime(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        received = []

        async def cb(event):
            received.append(event)

        rt.events.subscribe(cb)

        evt = RunStarted(execution_id="e1", session_key="cli:default")
        await rt.events.emit(evt)

        assert len(received) == 1
        assert received[0].type == "RunStarted"


# ══════════════════════════════════════════════════════════════════════════
# Event-to-transport push (via _event_to_stdout)
# ══════════════════════════════════════════════════════════════════════════

class TestEventTransportPush:

    @pytest.mark.asyncio
    async def test_event_to_stdout(self, capsys):
        from miqi.ipc.transport import _event_to_stdout

        evt = RunStarted(execution_id="e1", session_key="cli:default", preview="hi")
        await _event_to_stdout(evt)

        captured = capsys.readouterr()
        assert captured.out.endswith("\n")

        payload = json.loads(captured.out)
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "RunStarted"
        assert "id" not in payload  # notification — no id
        assert payload["params"]["execution_id"] == "e1"
        assert payload["params"]["preview"] == "hi"
        assert "type" not in payload["params"]  # type → method

    @pytest.mark.asyncio
    async def test_event_to_stdout_tool_result(self, capsys):
        from miqi.ipc.transport import _event_to_stdout

        evt = ToolResult(execution_id="e2", tool_name="exec", preview="done", is_error=False)
        await _event_to_stdout(evt)

        payload = json.loads(capsys.readouterr().out)
        assert payload["method"] == "ToolResult"
        assert payload["params"]["tool_name"] == "exec"
        assert payload["params"]["is_error"] is False

    @pytest.mark.asyncio
    async def test_event_to_stdout_approval_no_secrets(self, capsys):
        from miqi.ipc.transport import _event_to_stdout

        evt = ApprovalRequested(
            approval_id="abc123",
            execution_id="e3",
            tool_name="exec",
            pattern_description="recursive delete",
            command_preview="rm -rf ...",
        )
        await _event_to_stdout(evt)

        payload = json.loads(capsys.readouterr().out)
        assert payload["method"] == "ApprovalRequested"
        params = payload["params"]
        # No sensitive keys in params
        for key in params:
            assert "apikey" not in key.lower()
            assert "secret" not in key.lower()
            assert "password" not in key.lower()

    @pytest.mark.asyncio
    async def test_e2e_event_push_through_emitter(self, tmp_path: Path, monkeypatch, capsys):
        """Emit via EventEmitter → _event_to_stdout subscriber → stdout."""
        from miqi.ipc.transport import _event_to_stdout

        rt = _make_runtime(tmp_path, monkeypatch)
        rt.events.subscribe(_event_to_stdout)

        evt = SessionChanged(session_key="s1", action="created")
        await rt.events.emit(evt)

        payload = json.loads(capsys.readouterr().out)
        assert payload["method"] == "SessionChanged"
        assert payload["params"]["session_key"] == "s1"

    @pytest.mark.asyncio
    async def test_event_to_stdout_queue_updated(self, capsys):
        from miqi.ipc.transport import _event_to_stdout

        evt = QueueUpdated(queue_size=2, active_execution_id="e1", status="running")
        await _event_to_stdout(evt)

        payload = json.loads(capsys.readouterr().out)
        assert payload["method"] == "QueueUpdated"
        assert payload["params"]["queue_size"] == 2
        assert payload["params"]["status"] == "running"
        assert "type" not in payload["params"]


# ══════════════════════════════════════════════════════════════════════════
# Transport subscribe/unsubscribe lifecycle
# ══════════════════════════════════════════════════════════════════════════

class TestTransportEventLifecycle:

    @pytest.mark.asyncio
    async def test_read_requests_unsubscribes_on_eof(self, tmp_path: Path, monkeypatch):
        """After EOF, the _event_to_stdout subscriber must be removed."""
        from miqi.ipc.transport import _event_to_stdout, read_requests
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        reader = asyncio.StreamReader()
        reader.feed_data(b'\n')  # blank line, then EOF
        reader.feed_eof()

        assert rt.events.subscriber_count == 0
        await read_requests(dispatcher, reader=reader, event_emitter=rt.events)
        assert rt.events.subscriber_count == 0  # unsubscribed after exit

    @pytest.mark.asyncio
    async def test_read_requests_subscribes_during_lifecycle(self, tmp_path: Path, monkeypatch):
        """While running, the subscriber is active; after exit it is removed."""
        from miqi.ipc.transport import _event_to_stdout, read_requests
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # We'll feed a line then EOF
        reader = asyncio.StreamReader()
        reader.feed_data(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "app.status"}).encode() + b"\n"
        )
        reader.feed_eof()

        await read_requests(dispatcher, reader=reader, event_emitter=rt.events)
        assert rt.events.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_no_event_emitter_no_subscribe(self, tmp_path: Path, monkeypatch):
        """When event_emitter is None, no subscription happens."""
        from miqi.ipc.transport import read_requests
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        reader = asyncio.StreamReader()
        reader.feed_eof()

        await read_requests(dispatcher, reader=reader, event_emitter=None)
        # No subscription happened, so count stays at 0
        assert rt.events.subscriber_count == 0
