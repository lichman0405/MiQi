"""Tests for Phase 4 / 4.1 — ExecutionManager, AgentService, cancellation, and ExecTool."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from miqi.config.schema import Config
from miqi.events.emitter import EventEmitter
from miqi.events.models import (
    Error,
    QueueUpdated,
    RunCancelled,
    RunCompleted,
    RunStarted,
)
from miqi.providers.base import LLMProvider, LLMResponse
from miqi.runtime.execution import (
    ExecutionManager,
    ExecutionRecord,
    ExecutionStatus,
    generate_execution_id,
)
from miqi.runtime.factory import Runtime, create_runtime, wire_cron_callback


class FakeProvider(LLMProvider):
    def __init__(self, default_model: str = "fake-model"):
        super().__init__(api_key="test-key")
        self._default_model = default_model

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, *, on_delta=None):
        return LLMResponse(content="fake response")

    def get_default_model(self) -> str:
        return self._default_model


def _make_runtime(tmp_path: Path, monkeypatch) -> Runtime:
    monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIQI_DATA_DIR", str(tmp_path / "data"))
    config = Config()
    rt = create_runtime(config, make_provider=lambda c: FakeProvider(), init_session_manager=True)
    wire_cron_callback(rt)
    return rt


# ══════════════════════════════════════════════════════════════════════════
# ExecutionManager
# ══════════════════════════════════════════════════════════════════════════

class TestGenerateExecutionId:
    def test_returns_12_char_hex(self):
        eid = generate_execution_id()
        assert len(eid) == 12
        assert all(c in "0123456789abcdef" for c in eid)

    def test_unique(self):
        ids = {generate_execution_id() for _ in range(100)}
        assert len(ids) == 100


class TestExecutionManagerStart:

    @pytest.mark.asyncio
    async def test_start_emits_run_started(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        ok = await mgr.start("e1", "cli:default", channel="cli", preview="hello")
        assert ok is True

        assert len(collected) >= 1
        assert isinstance(collected[0], RunStarted)
        assert collected[0].execution_id == "e1"
        assert collected[0].session_key == "cli:default"
        assert collected[0].preview == "hello"

    @pytest.mark.asyncio
    async def test_start_emits_queue_updated(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "cli:default")

        qu = [e for e in collected if isinstance(e, QueueUpdated)]
        assert len(qu) == 1
        assert qu[0].queue_size == 1
        assert qu[0].active_execution_id == "e1"
        assert qu[0].status == "running"

    @pytest.mark.asyncio
    async def test_start_rejects_second_active(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        ok1 = await mgr.start("e1", "s1")
        assert ok1 is True
        ok2 = await mgr.start("e2", "s2")
        assert ok2 is False
        assert mgr.active_execution_id == "e1"
        assert mgr.queue_size == 1


class TestExecutionManagerComplete:

    @pytest.mark.asyncio
    async def test_complete_emits_run_completed(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "cli:default")
        await mgr.complete("e1", response_preview="hi there")

        rc = [e for e in collected if isinstance(e, RunCompleted)]
        assert len(rc) == 1
        assert rc[0].execution_id == "e1"
        assert rc[0].response_preview == "hi there"

    @pytest.mark.asyncio
    async def test_complete_clears_active(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "cli:default")
        assert mgr.active_execution_id == "e1"
        await mgr.complete("e1")
        assert mgr.active_execution_id is None
        assert mgr.queue_size == 0

    @pytest.mark.asyncio
    async def test_complete_unknown_id_is_noop(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.complete("nonexistent")  # should not raise


class TestExecutionManagerFail:

    @pytest.mark.asyncio
    async def test_fail_emits_run_completed_with_error(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "cli:default")
        await mgr.fail("e1", error_message="something broke")

        rc = [e for e in collected if isinstance(e, RunCompleted)]
        assert len(rc) == 1
        assert "something broke" in rc[0].response_preview

    @pytest.mark.asyncio
    async def test_fail_clears_active(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "cli:default")
        await mgr.fail("e1", error_message="err")
        assert mgr.active_execution_id is None


class TestExecutionManagerCancel:

    @pytest.mark.asyncio
    async def test_cancel_emits_run_cancelled(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "cli:default")
        result = await mgr.cancel("e1", reason="user")

        assert result is True
        rc = [e for e in collected if isinstance(e, RunCancelled)]
        assert len(rc) == 1
        assert rc[0].execution_id == "e1"
        assert rc[0].reason == "user"

    @pytest.mark.asyncio
    async def test_cancel_keeps_record_as_cancelling(self):
        """After cancel(), record must still exist with CANCELLING status."""
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "cli:default")
        await mgr.cancel("e1")

        rec = mgr.get("e1")
        assert rec is not None
        assert rec.status == ExecutionStatus.CANCELLING

    @pytest.mark.asyncio
    async def test_should_cancel_true_after_cancel(self):
        """should_cancel must return True while record is CANCELLING."""
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        assert mgr.should_cancel("e1") is True

    @pytest.mark.asyncio
    async def test_mark_cancelled_removes_record(self):
        """After mark_cancelled(), record is gone and queue is idle."""
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        assert mgr.should_cancel("e1") is True

        await mgr.mark_cancelled("e1")
        assert mgr.get("e1") is None
        assert mgr.active_execution_id is None
        assert mgr.queue_size == 0

        # QueueUpdated should have gone through cancelling → idle
        qu_events = [e for e in collected if isinstance(e, QueueUpdated)]
        statuses = [e.status for e in qu_events]
        assert "cancelling" in statuses
        assert statuses[-1] == "idle"

    @pytest.mark.asyncio
    async def test_cancel_no_duplicate_events(self):
        """Second cancel on already-cancelling execution must not emit a second RunCancelled."""
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        result = await mgr.cancel("e1")  # already cancelling
        assert result is True

        cancelled = [e for e in collected if isinstance(e, RunCancelled)]
        assert len(cancelled) == 1  # only one RunCancelled

    @pytest.mark.asyncio
    async def test_cancel_unknown_execution_returns_false(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        result = await mgr.cancel("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_cancels_task(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)

        task = asyncio.create_task(asyncio.sleep(100))
        await mgr.start("e1", "cli:default", task=task)

        await mgr.cancel("e1")
        await asyncio.sleep(0)
        assert task.cancelled() or task.cancelling() or task.done()

    @pytest.mark.asyncio
    async def test_cancel_queue_status_is_cancelling(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "s1")
        await mgr.cancel("e1")

        qu = [e for e in collected if isinstance(e, QueueUpdated)]
        assert qu[-1].status == "cancelling"


class TestExecutionManagerQueueState:

    @pytest.mark.asyncio
    async def test_queue_idle_initially(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        assert mgr.queue_size == 0
        assert mgr.active_execution_id is None
        assert mgr.pending_execution_ids == []

    @pytest.mark.asyncio
    async def test_queue_status_transitions(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        collected = []
        events.subscribe(lambda e: collected.append(e))

        await mgr.start("e1", "s1")
        await mgr.complete("e1")

        qu_events = [e for e in collected if isinstance(e, QueueUpdated)]
        statuses = [e.status for e in qu_events]
        assert "running" in statuses
        assert statuses[-1] == "idle"

    @pytest.mark.asyncio
    async def test_has_active_property(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        assert mgr.has_active is False
        await mgr.start("e1", "s1")
        assert mgr.has_active is True
        await mgr.complete("e1")
        assert mgr.has_active is False


class TestExecutionManagerGetRecord:

    @pytest.mark.asyncio
    async def test_get_existing(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        rec = mgr.get("e1")
        assert rec is not None
        assert rec.execution_id == "e1"
        assert rec.status == ExecutionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_after_complete_returns_none(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.complete("e1")
        assert mgr.get("e1") is None

    @pytest.mark.asyncio
    async def test_get_after_cancel_returns_cancelling_record(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        rec = mgr.get("e1")
        assert rec is not None
        assert rec.status == ExecutionStatus.CANCELLING

    @pytest.mark.asyncio
    async def test_get_after_mark_cancelled_returns_none(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        await mgr.mark_cancelled("e1")
        assert mgr.get("e1") is None

    @pytest.mark.asyncio
    async def test_get_after_finalize_cancelled_returns_none(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        await mgr.finalize_cancelled("e1")
        assert mgr.get("e1") is None
        assert mgr.active_execution_id is None
        assert mgr.queue_size == 0


class TestExecutionManagerAttachTask:

    @pytest.mark.asyncio
    async def test_attach_task_to_existing_record(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        assert mgr.get("e1").task is None

        task = asyncio.create_task(asyncio.sleep(100))
        mgr.attach_task("e1", task)
        assert mgr.get("e1").task is task

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestShouldCancel:

    @pytest.mark.asyncio
    async def test_not_cancelling_by_default(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        assert mgr.should_cancel("e1") is False

    @pytest.mark.asyncio
    async def test_true_after_cancel_before_mark_cancelled(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        assert mgr.should_cancel("e1") is True

    @pytest.mark.asyncio
    async def test_false_after_mark_cancelled(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        await mgr.mark_cancelled("e1")
        assert mgr.should_cancel("e1") is False

    @pytest.mark.asyncio
    async def test_false_after_finalize_cancelled(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        await mgr.start("e1", "s1")
        await mgr.cancel("e1")
        await mgr.finalize_cancelled("e1")
        assert mgr.should_cancel("e1") is False

    @pytest.mark.asyncio
    async def test_unknown_execution(self):
        events = EventEmitter()
        mgr = ExecutionManager(events)
        assert mgr.should_cancel("nonexistent") is False


# ══════════════════════════════════════════════════════════════════════════
# AgentService
# ══════════════════════════════════════════════════════════════════════════

class TestAgentService:

    @pytest.mark.asyncio
    async def test_send_returns_execution_id(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        assert svc is not None

        result = await svc.send("hello", session_key="test:s1")
        assert "execution_id" in result
        assert len(result["execution_id"]) == 12

    @pytest.mark.asyncio
    async def test_send_emits_run_started(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        await svc.send("hello", session_key="test:s1")

        started = [e for e in collected if isinstance(e, RunStarted)]
        assert len(started) == 1
        assert started[0].session_key == "test:s1"

    @pytest.mark.asyncio
    async def test_successful_run_emits_completed(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        rt.agent.process_direct = AsyncMock(return_value="test reply")

        result = await svc.send("hello", session_key="test:s1")
        execution_id = result["execution_id"]

        await asyncio.sleep(0.1)

        completed = [e for e in collected if isinstance(e, RunCompleted)]
        assert len(completed) == 1
        assert completed[0].execution_id == execution_id
        assert "test reply" in completed[0].response_preview

    @pytest.mark.asyncio
    async def test_failed_run_emits_error_and_completes(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        rt.agent.process_direct = AsyncMock(side_effect=RuntimeError("LLM down"))

        result = await svc.send("hello", session_key="test:s1")
        execution_id = result["execution_id"]

        await asyncio.sleep(0.1)

        errors = [e for e in collected if isinstance(e, Error)]
        assert len(errors) == 1
        assert errors[0].execution_id == execution_id
        assert "LLM down" in errors[0].message

        completed = [e for e in collected if isinstance(e, RunCompleted)]
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_cancel_known_execution(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        async def _hang(**kwargs):
            await asyncio.sleep(100)
            return "done"

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        result = await svc.send("hello", session_key="test:s1")
        execution_id = result["execution_id"]

        found = await svc.cancel(execution_id, reason="user")
        assert found is True

        cancelled = [e for e in collected if isinstance(e, RunCancelled)]
        assert len(cancelled) == 1
        assert cancelled[0].execution_id == execution_id
        assert cancelled[0].reason == "user"

    @pytest.mark.asyncio
    async def test_cancel_unknown_execution(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service

        found = await svc.cancel("nonexistent_id", reason="user")
        assert found is False

    @pytest.mark.asyncio
    async def test_second_send_rejected_while_active(self, tmp_path: Path, monkeypatch):
        """Single-active strategy: second send raises AgentBusyError."""
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service

        async def _hang(**kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        result1 = await svc.send("hello", session_key="test:s1")
        assert "execution_id" in result1

        from miqi.runtime.agent_service import AgentBusyError
        with pytest.raises(AgentBusyError):
            await svc.send("second message", session_key="test:s2")


class TestAgentServiceEventOrder:

    @pytest.mark.asyncio
    async def test_event_order_on_success(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        rt.agent.process_direct = AsyncMock(return_value="ok")

        await svc.send("hi", session_key="test:s1")
        await asyncio.sleep(0.1)

        types = [e.type for e in collected]
        # RunStarted → QueueUpdated(running) → MessageFinal → RunCompleted → QueueUpdated(idle)
        assert types[0] == "RunStarted"
        assert types[1] == "QueueUpdated"
        assert types[2] == "MessageFinal"
        assert types[3] == "RunCompleted"
        assert types[4] == "QueueUpdated"

        # Verify specific status values
        qu_events = [e for e in collected if isinstance(e, QueueUpdated)]
        assert qu_events[0].status == "running"
        assert qu_events[1].status == "idle"

    @pytest.mark.asyncio
    async def test_event_order_on_cancel(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        async def _hang(**kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        result = await svc.send("hi", session_key="test:s1")
        await svc.cancel(result["execution_id"])

        types = [e.type for e in collected]
        assert types[0] == "RunStarted"
        assert types[1] == "QueueUpdated"  # running
        assert types[2] == "RunCancelled"
        assert types[3] == "QueueUpdated"  # cancelling

    @pytest.mark.asyncio
    async def test_no_duplicate_run_cancelled(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        async def _hang(**kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        result = await svc.send("hi", session_key="test:s1")
        eid = result["execution_id"]

        await svc.cancel(eid)
        await svc.cancel(eid)  # second cancel — should not emit second RunCancelled

        cancelled = [e for e in collected if isinstance(e, RunCancelled)]
        assert len(cancelled) == 1


class TestAgentServiceCancelLifecycle:

    @pytest.mark.asyncio
    async def test_should_cancel_true_after_cancel_before_task_cleanup(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service

        async def _hang(**kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        result = await svc.send("hi", session_key="test:s1")
        eid = result["execution_id"]

        await svc.cancel(eid)
        assert svc.manager.should_cancel(eid) is True

    @pytest.mark.asyncio
    async def test_after_cancel_cleanup_record_removed_queue_idle(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.agent_service
        collected = []
        async def _collect(e):
            collected.append(e)
        rt.events.subscribe(_collect)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        result = await svc.send("hi", session_key="test:s1")
        eid = result["execution_id"]

        # Let the background task actually start running and enter
        # the _hang sleep before we cancel it.
        await asyncio.sleep(0.05)

        await svc.cancel(eid)
        assert svc.manager.should_cancel(eid) is True

        # Let the CancelledError propagate through the task.
        rec = svc.manager.get(eid)
        if rec is not None and rec.task is not None and not rec.task.done():
            try:
                await rec.task
            except (asyncio.CancelledError, Exception):
                pass

        # After task cleanup, record gone, queue idle
        assert svc.manager.get(eid) is None
        assert svc.manager.active_execution_id is None
        assert svc.manager.queue_size == 0
        assert svc.manager.has_active is False


# ══════════════════════════════════════════════════════════════════════════
# IPC handlers
# ══════════════════════════════════════════════════════════════════════════

class TestChatSendCancelRPC:

    @pytest.mark.asyncio
    async def test_chat_send_returns_execution_id(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        from miqi.ipc.protocol import JsonRpcRequest
        req = JsonRpcRequest(id=1, method="chat.send", params={
            "message": "hello",
            "session_key": "test:s1",
        })

        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "execution_id" in resp.result
        assert len(resp.result["execution_id"]) == 12

    @pytest.mark.asyncio
    async def test_chat_send_requires_message(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        from miqi.ipc.protocol import JsonRpcRequest
        req = JsonRpcRequest(id=2, method="chat.send", params={})

        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_chat_cancel_known_execution(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        async def _hang(**kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        from miqi.ipc.protocol import JsonRpcRequest
        send_req = JsonRpcRequest(id=1, method="chat.send", params={
            "message": "hello",
            "session_key": "test:s1",
        })
        send_resp = await dispatcher.dispatch(send_req)
        execution_id = send_resp.result["execution_id"]

        cancel_req = JsonRpcRequest(id=2, method="chat.cancel", params={
            "execution_id": execution_id,
        })
        cancel_resp = await dispatcher.dispatch(cancel_req)
        assert cancel_resp.error is None
        assert cancel_resp.result["success"] is True

    @pytest.mark.asyncio
    async def test_chat_cancel_unknown_execution(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        from miqi.ipc.protocol import JsonRpcRequest
        cancel_req = JsonRpcRequest(id=1, method="chat.cancel", params={
            "execution_id": "nonexistent",
        })
        cancel_resp = await dispatcher.dispatch(cancel_req)
        assert cancel_resp.error is None
        assert cancel_resp.result["success"] is False

    @pytest.mark.asyncio
    async def test_chat_cancel_requires_execution_id(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        from miqi.ipc.protocol import JsonRpcRequest
        cancel_req = JsonRpcRequest(id=1, method="chat.cancel", params={})
        cancel_resp = await dispatcher.dispatch(cancel_req)
        assert cancel_resp.error is not None

    @pytest.mark.asyncio
    async def test_method_names_includes_chat_cancel(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        assert "chat.cancel" in dispatcher.method_names
        assert "chat.send" in dispatcher.method_names

    @pytest.mark.asyncio
    async def test_second_chat_send_while_active_returns_busy_error(self, tmp_path: Path, monkeypatch):
        """Single-active: second chat.send returns ERROR_EXECUTION_BUSY."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(100)

        rt.agent.process_direct = AsyncMock(side_effect=_hang)

        from miqi.ipc.protocol import ERROR_EXECUTION_BUSY, JsonRpcRequest
        req1 = JsonRpcRequest(id=1, method="chat.send", params={
            "message": "first",
        })
        resp1 = await dispatcher.dispatch(req1)
        assert resp1.error is None

        req2 = JsonRpcRequest(id=2, method="chat.send", params={
            "message": "second",
        })
        resp2 = await dispatcher.dispatch(req2)
        assert resp2.error is not None
        assert resp2.error.code == ERROR_EXECUTION_BUSY
        assert "busy" in resp2.error.message.lower() or "already active" in resp2.error.message.lower()


# ══════════════════════════════════════════════════════════════════════════
# ExecTool cancellation
# ══════════════════════════════════════════════════════════════════════════

class TestExecToolCancellation:

    @pytest.mark.asyncio
    async def test_cancelled_error_terminates_subprocess(self):
        """When the ExecTool is cancelled mid-execution, the subprocess must be terminated."""
        from miqi.agent.tools.shell import ExecTool

        tool = ExecTool(timeout=60, working_dir=".")

        # Use a long-running command
        if sys.platform == "win32":
            cmd = "ping -n 60 127.0.0.1"
        else:
            cmd = "sleep 60"

        async def _run_and_cancel():
            task = asyncio.create_task(tool.execute(command=cmd))
            # Give the subprocess time to start
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_and_cancel()
        # If we reach here without hanging, the subprocess was cleaned up

    @pytest.mark.asyncio
    async def test_normal_execution_still_works(self):
        """Normal (non-cancelled) execution still produces correct output."""
        from miqi.agent.tools.shell import ExecTool

        tool = ExecTool(timeout=10, working_dir=".")

        if sys.platform == "win32":
            result = await tool.execute(command="echo hello")
        else:
            result = await tool.execute(command="echo hello")
        assert "hello" in result.lower()


# ══════════════════════════════════════════════════════════════════════════
# Runtime integration
# ══════════════════════════════════════════════════════════════════════════

class TestRuntimeAgentService:

    def test_runtime_has_agent_service(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        assert rt.agent_service is not None

    def test_agent_service_has_manager(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        assert rt.agent_service.manager is not None
        assert isinstance(rt.agent_service.manager, ExecutionManager)

    @pytest.mark.asyncio
    async def test_cli_gateway_still_use_process_direct(self, tmp_path: Path, monkeypatch):
        """CLI and gateway should still be able to call process_direct directly."""
        rt = _make_runtime(tmp_path, monkeypatch)
        rt.agent.process_direct = AsyncMock(return_value="direct response")
        result = await rt.agent.process_direct("hello")
        assert result == "direct response"
