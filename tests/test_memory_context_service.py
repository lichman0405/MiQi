"""Tests for Phase 7.3: MemoryService, ContextService, and their IPC handlers.

Covers:
- MemoryService: status, search, remember, append_today, learn_lesson,
  delete snapshot/lesson, list snapshot/lessons
- ContextService: status, list_bootstrap, read_bootstrap, list_skills, budget
- IPC handler dispatch for memory.* and context.* methods
"""

from __future__ import annotations

import pytest

from miqi.config.schema import Config
from miqi.ipc.protocol import JsonRpcRequest
from miqi.providers.base import LLMProvider, LLMResponse
from miqi.runtime.factory import Runtime, create_runtime, wire_cron_callback
from miqi.runtime.memory_service import MemoryService
from miqi.runtime.context_service import ContextService
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────

class FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test-key")

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, *, on_delta=None):
        return LLMResponse(content="fake")

    def get_default_model(self) -> str:
        return "fake-model"


def _make_fake_provider(config: Config) -> FakeProvider:
    return FakeProvider()


def _make_runtime(tmp_path: Path, monkeypatch) -> Runtime:
    monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIQI_DATA_DIR", str(tmp_path / "data"))
    config = Config()
    rt = create_runtime(config, make_provider=_make_fake_provider, init_session_manager=True)
    wire_cron_callback(rt)
    return rt


# ══════════════════════════════════════════════════════════════════════════
# MemoryService unit tests
# ══════════════════════════════════════════════════════════════════════════

class TestMemoryServiceStatus:

    def test_status_returns_keys(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.status()
        assert "ltm_items" in result
        assert "lessons_count" in result
        assert "self_improvement_enabled" in result

    def test_status_redacts_paths(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.status()
        # Paths should be redacted
        assert result.get("snapshot_path") == "..."
        assert result.get("audit_path") == "..."


class TestMemoryServiceSearch:

    def test_search_empty_query(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.search("")
        assert result["count"] == 0

    def test_search_no_results(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.search("nonexistent_xyz_12345")
        assert result["count"] == 0

    def test_search_finds_snapshot_item(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        rt.agent.memory.remember("quantum computing basics", session_key="test:search", source="test", immediate=True)
        result = svc.search("quantum")
        assert result["count"] >= 1
        assert any(r["source"] == "snapshot" for r in result["results"])

    def test_search_finds_lesson(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        rt.agent.memory.learn_lesson("use async", "", "prefer async/await", session_key="test:search", source="test", immediate=True)
        result = svc.search("async")
        assert result["count"] >= 1
        assert any(r["source"] == "lesson" for r in result["results"])

    def test_search_finds_daily_note(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        rt.agent.memory.append_today("Explored webhook integration patterns")
        rt.agent.memory.flush_if_needed()
        result = svc.search("webhook")
        assert result["count"] >= 1
        assert any(r["source"] == "daily_note" for r in result["results"])

    def test_search_respects_limit(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        for i in range(10):
            rt.agent.memory.remember(f"item_{i:03d} unique_token_{i}", session_key="test:search", source="test")
        rt.agent.memory.flush_if_needed()
        result = svc.search("unique_token", limit=3)
        assert result["count"] <= 3


class TestMemoryServiceRemember:

    def test_remember_adds_item(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.remember("test memory content")
        assert result["action"] == "remember"
        assert result["text_length"] > 0

    def test_remember_empty_raises(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        with pytest.raises(ValueError, match="must not be empty"):
            svc.remember("")


class TestMemoryServiceAppendToday:

    def test_append_today(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.append_today("daily update content")
        assert result["action"] == "append_today"

    def test_append_today_empty_raises(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        with pytest.raises(ValueError, match="must not be empty"):
            svc.append_today("")


class TestMemoryServiceLearnLesson:

    def test_learn_lesson(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.learn_lesson("use type hints", "Always add type hints to functions")
        assert result["action"] == "learn_lesson"

    def test_learn_lesson_empty_trigger_raises(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        with pytest.raises(ValueError, match="trigger must not be empty"):
            svc.learn_lesson("", "some action")

    def test_learn_lesson_empty_action_raises(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        with pytest.raises(ValueError, match="better_action must not be empty"):
            svc.learn_lesson("some trigger", "")


class TestMemoryServiceDelete:

    def test_delete_snapshot_item(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        rt.agent.memory.remember("item to delete", session_key="test:del", source="test")
        rt.agent.memory.flush_if_needed()
        items = rt.agent.memory.list_snapshot_items(limit=50)
        if items:
            result = svc.delete_snapshot_item(items[0].get("id", "unknown"))
            assert result["action"] == "delete_snapshot_item"

    def test_delete_lesson(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        rt.agent.memory.learn_lesson("test trigger", "", "test action", session_key="test:del", source="test", immediate=True)
        lessons = rt.agent.memory.list_lessons(include_disabled=True, limit=50)
        if lessons:
            result = svc.delete_lesson(lessons[0].get("id", "unknown"))
            assert result["action"] == "delete_lesson"


class TestMemoryServiceList:

    def test_list_snapshot_items(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.list_snapshot_items()
        assert "items" in result
        assert "count" in result

    def test_list_lessons(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        result = svc.list_lessons()
        assert "lessons" in result
        assert "count" in result

    def test_set_lesson_enabled(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.memory_service
        rt.agent.memory.learn_lesson("toggle test", "", "toggle action", session_key="test:toggle", source="test", immediate=True)
        lessons = rt.agent.memory.list_lessons(include_disabled=True, limit=50)
        if lessons:
            result = svc.set_lesson_enabled(lessons[0].get("id", "unknown"), False)
            assert result["enabled"] is False


# ══════════════════════════════════════════════════════════════════════════
# ContextService unit tests
# ══════════════════════════════════════════════════════════════════════════

class TestContextServiceStatus:

    def test_status_returns_overview(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.status()
        assert "workspace" in result
        assert "bootstrap_files" in result
        assert "skills" in result
        assert "memory" in result
        assert "pinned_files" in result
        assert "budget" in result

    def test_status_bootstrap_files_shape(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.status()
        for f in result["bootstrap_files"]:
            assert "name" in f
            assert "exists" in f
            assert "size" in f
            assert "source" in f

    def test_status_memory_summary(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.status()
        mem = result["memory"]
        assert "ltm_items" in mem
        assert "lessons_count" in mem

    def test_status_budget(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.status()
        budget = result["budget"]
        assert "context_limit_chars" in budget
        assert "estimated_usage" in budget


class TestContextServiceBootstrap:

    def test_list_bootstrap(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.list_bootstrap()
        assert result["count"] > 0
        names = [f["name"] for f in result["files"]]
        assert "AGENTS.md" in names

    def test_tools_md_exists_from_system_template(self, tmp_path: Path, monkeypatch):
        """TOOLS.md should exist=True even in empty workspace (from package template)."""
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.list_bootstrap()
        tools_entry = next(f for f in result["files"] if f["name"] == "TOOLS.md")
        assert tools_entry["exists"] is True
        assert tools_entry["source"] == "system"
        assert tools_entry["size"] > 0

    def test_read_tools_md_system_template(self, tmp_path: Path, monkeypatch):
        """readBootstrap(TOOLS.md) should return package template content in empty workspace."""
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.read_bootstrap("TOOLS.md")
        assert result["exists"] is True
        assert result["source"] == "system"
        assert result["content"] is not None
        assert len(result["content"]) > 0
        assert result["has_workspace_override"] is False

    def test_read_tools_md_workspace_override(self, tmp_path: Path, monkeypatch):
        """When workspace has its own TOOLS.md, system + override are combined."""
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "TOOLS.md").write_text("# Custom Tools\nMy custom tools.", encoding="utf-8")
        svc = rt.context_service
        result = svc.read_bootstrap("TOOLS.md")
        assert result["exists"] is True
        assert result["source"] == "system"
        assert result["has_workspace_override"] is True
        # Content should contain both system and workspace
        assert "Custom Tools" in result["content"]

    def test_read_tools_md_workspace_same_as_system(self, tmp_path: Path, monkeypatch):
        """If workspace TOOLS.md is identical to system, no override is flagged."""
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        # Read system template
        from miqi.runtime.context_service import _read_system_template
        sys_content = _read_system_template("TOOLS.md")
        if sys_content:
            (tmp_path / "TOOLS.md").write_text(sys_content, encoding="utf-8")
            result = svc.read_bootstrap("TOOLS.md")
            assert result["has_workspace_override"] is False

    def test_read_bootstrap_nonexistent(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.read_bootstrap("AGENTS.md")
        assert "name" in result
        assert "exists" in result
        assert result["source"] == "none"

    def test_read_bootstrap_existing(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "AGENTS.md").write_text("# Agent Rules\nBe helpful.", encoding="utf-8")
        svc = rt.context_service
        result = svc.read_bootstrap("AGENTS.md")
        assert result["exists"] is True
        assert result["source"] == "workspace"
        assert "Be helpful" in result["content"]

    def test_read_bootstrap_unknown_name_raises(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        with pytest.raises(ValueError, match="unknown bootstrap file"):
            svc.read_bootstrap("NONEXISTENT.md")

    def test_read_bootstrap_truncates_large_file(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "AGENTS.md").write_text("x" * 20000, encoding="utf-8")
        svc = rt.context_service
        result = svc.read_bootstrap("AGENTS.md")
        assert result["truncated"] is True
        assert len(result["content"]) <= 8192


class TestContextServiceSkills:

    def test_list_skills(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        svc = rt.context_service
        result = svc.list_skills()
        assert "skills" in result
        assert "count" in result


# ══════════════════════════════════════════════════════════════════════════
# IPC handler tests
# ══════════════════════════════════════════════════════════════════════════

class TestMemoryIpcHandlers:

    @pytest.mark.asyncio
    async def test_memory_status(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=1, method="memory.status")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "ltm_items" in resp.result

    @pytest.mark.asyncio
    async def test_memory_search(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=2, method="memory.search", params={"query": ""})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 0

    @pytest.mark.asyncio
    async def test_memory_remember(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=3, method="memory.remember", params={"text": "test memory"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "remember"

    @pytest.mark.asyncio
    async def test_memory_remember_missing_text(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=4, method="memory.remember", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_memory_update_remember(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=20, method="memory.update", params={"text": "remembered via update", "action": "remember"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "remember"

    @pytest.mark.asyncio
    async def test_memory_update_append_today(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=21, method="memory.update", params={"text": "daily via update", "action": "append_today"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "append_today"

    @pytest.mark.asyncio
    async def test_memory_update_learn_lesson(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=22, method="memory.update", params={
            "text": "slow queries", "action": "learn_lesson", "better_action": "add indexes"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "learn_lesson"

    @pytest.mark.asyncio
    async def test_memory_update_default_action_is_remember(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=23, method="memory.update", params={"text": "default action"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "remember"

    @pytest.mark.asyncio
    async def test_memory_update_invalid_action(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=24, method="memory.update", params={"text": "test", "action": "invalid"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_memory_update_missing_text(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=25, method="memory.update", params={"action": "remember"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_memory_append_today(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=5, method="memory.appendToday", params={"content": "daily update"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "append_today"

    @pytest.mark.asyncio
    async def test_memory_learn_lesson(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=6, method="memory.learnLesson", params={
            "trigger": "slow code", "better_action": "use caching"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["action"] == "learn_lesson"

    @pytest.mark.asyncio
    async def test_memory_list_snapshot(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=7, method="memory.listSnapshot")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "items" in resp.result

    @pytest.mark.asyncio
    async def test_memory_list_lessons(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=8, method="memory.listLessons")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "lessons" in resp.result


class TestContextIpcHandlers:

    @pytest.mark.asyncio
    async def test_context_status(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=10, method="context.status")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "bootstrap_files" in resp.result

    @pytest.mark.asyncio
    async def test_context_list_bootstrap(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=11, method="context.listBootstrap")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] > 0

    @pytest.mark.asyncio
    async def test_context_read_bootstrap(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        (tmp_path / "AGENTS.md").write_text("# Test", encoding="utf-8")
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=12, method="context.readBootstrap", params={"name": "AGENTS.md"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["exists"] is True

    @pytest.mark.asyncio
    async def test_context_list_skills(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=13, method="context.listSkills")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "skills" in resp.result

    @pytest.mark.asyncio
    async def test_all_methods_registered(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        names = dispatcher.method_names
        for method in [
            "memory.status", "memory.search", "memory.update", "memory.remember",
            "memory.appendToday", "memory.learnLesson",
            "memory.listSnapshot", "memory.listLessons",
            "memory.deleteSnapshotItem", "memory.deleteLesson",
            "memory.setLessonEnabled",
            "context.status", "context.listBootstrap",
            "context.readBootstrap", "context.listSkills",
        ]:
            assert method in names, f"method {method} not registered"
