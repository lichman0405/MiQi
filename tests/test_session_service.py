"""Tests for Phase 7.1: SessionService and session IPC handlers.

Covers:
- JSONL compatibility (SessionService wraps SessionManager without changing storage)
- session.list shape: key/title/preview/source/updated_at/message_count
- session.create with and without title
- session.rename
- session.delete (existing and non-existing)
- session.search (title and content match, empty query, no results)
- session.load (message sanitization, no internal objects leaked)
- Empty sessions
- Bad parameters (missing key, empty key, missing title)
- IPC response shapes via RpcDispatcher
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from miqi.config.schema import Config
from miqi.ipc.protocol import (
    ERROR_INVALID_PARAMS,
    ERROR_INTERNAL,
    ERROR_METHOD_NOT_FOUND,
    JsonRpcRequest,
)
from miqi.providers.base import LLMProvider, LLMResponse
from miqi.runtime.factory import Runtime, create_runtime, wire_cron_callback
from miqi.runtime.session_service import SessionService
from miqi.session.manager import Session, SessionManager


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


def _seed_session(sm: SessionManager, key: str, messages: list[dict] | None = None, title: str | None = None) -> Session:
    """Create and save a session with optional messages and title."""
    session = sm.get_or_create(key)
    if title:
        session.metadata["title"] = title
    if messages:
        session.messages.extend(messages)
    sm.save(session)
    sm.invalidate(key)  # Force reload from disk
    return session


# ══════════════════════════════════════════════════════════════════════════
# SessionService unit tests
# ══════════════════════════════════════════════════════════════════════════

class TestSessionServiceList:

    def test_empty_workspace(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        result = svc.list_sessions()
        assert result["count"] == 0
        assert result["sessions"] == []

    def test_list_shape(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ], title="My Chat")
        svc = SessionService(sm)
        result = svc.list_sessions()
        assert result["count"] == 1
        item = result["sessions"][0]
        assert "key" in item
        assert "title" in item
        assert "preview" in item
        assert "source" in item
        assert "updated_at" in item
        assert "message_count" in item
        assert item["key"] == "desktop:chat1"
        assert item["title"] == "My Chat"
        assert item["preview"] == "hi there"
        assert item["source"] == "desktop"
        assert item["message_count"] == 2

    def test_list_title_fallback(self, tmp_path: Path):
        """When no title metadata, first user message becomes title."""
        sm = SessionManager(tmp_path)
        _seed_session(sm, "cli:direct", [
            {"role": "user", "content": "what is 2+2?"},
        ])
        svc = SessionService(sm)
        result = svc.list_sessions()
        assert result["count"] == 1
        assert result["sessions"][0]["title"] == "what is 2+2?"

    def test_list_source_derived_from_key(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "feishu:group123", [])
        svc = SessionService(sm)
        result = svc.list_sessions()
        assert result["sessions"][0]["source"] == "feishu"

    def test_list_updated_at_freshness(self, tmp_path: Path):
        """updated_at must reflect the latest message, not stale metadata."""
        import time
        sm = SessionManager(tmp_path)
        # Create session with initial message
        _seed_session(sm, "desktop:fresh", [
            {"role": "user", "content": "first"},
        ])
        # Record the listed updated_at
        svc = SessionService(sm)
        first_result = svc.list_sessions()
        first_updated = first_result["sessions"][0]["updated_at"]

        # Add a new message via add_message (which updates session.updated_at)
        time.sleep(0.05)  # Ensure timestamp differs
        session = sm.get_or_create("desktop:fresh")
        session.add_message("user", "second message")
        sm.save(session)

        second_result = svc.list_sessions()
        second_updated = second_result["sessions"][0]["updated_at"]
        assert second_updated > first_updated

    def test_list_sorted_by_updated_at_descending(self, tmp_path: Path):
        import time
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:old", [
            {"role": "user", "content": "old message"},
        ])
        time.sleep(0.05)
        _seed_session(sm, "desktop:new", [
            {"role": "user", "content": "new message"},
        ])
        svc = SessionService(sm)
        result = svc.list_sessions()
        assert result["count"] == 2
        # Most recently updated session first
        assert result["sessions"][0]["key"] == "desktop:new"


class TestSessionServiceCreate:

    def test_create_with_title(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        result = svc.create_session("desktop:new1", title="New Session")
        assert result["key"] == "desktop:new1"
        assert result["title"] == "New Session"
        assert result["message_count"] == 0
        # Verify persisted
        session = sm.get_or_create("desktop:new1")
        assert session.metadata.get("title") == "New Session"

    def test_create_without_title(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        result = svc.create_session("desktop:new2")
        assert result["key"] == "desktop:new2"
        assert result["title"] == "desktop:new2"  # key as fallback
        assert result["message_count"] == 0

    def test_create_empty_key_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="key must not be empty"):
            svc.create_session("")

    def test_create_existing_key_is_idempotent(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        svc.create_session("desktop:test", title="First")
        result = svc.create_session("desktop:test", title="Updated")
        assert result["title"] == "Updated"

    def test_create_existing_key_persists_title_to_disk(self, tmp_path: Path):
        """create_session(existing_key, title=...) must persist title via full rewrite."""
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        svc.create_session("desktop:persist", title="First Title")
        # Add a message so the session has history (append-only save scenario)
        session = sm.get_or_create("desktop:persist")
        session.add_message("user", "hello")
        sm.save(session)

        # Now update title on existing key
        svc.create_session("desktop:persist", title="Updated Title")

        # Verify on disk via fresh SessionManager
        sm2 = SessionManager(tmp_path)
        reloaded = sm2.get_or_create("desktop:persist")
        assert reloaded.metadata.get("title") == "Updated Title"

    def test_create_strips_title_whitespace(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        result = svc.create_session("desktop:ws", title="  Spaced Title  ")
        assert result["title"] == "Spaced Title"


class TestSessionServiceRename:

    def test_rename_existing(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Old Title")
        svc = SessionService(sm)
        result = svc.rename_session("desktop:chat1", "New Title")
        assert result["key"] == "desktop:chat1"
        assert result["title"] == "New Title"
        # Verify persisted
        sm.invalidate("desktop:chat1")
        session = sm.get_or_create("desktop:chat1")
        assert session.metadata["title"] == "New Title"

    def test_rename_empty_key_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="key must not be empty"):
            svc.rename_session("", "title")

    def test_rename_empty_title_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="title must not be empty"):
            svc.rename_session("desktop:chat1", "")

    def test_rename_strips_whitespace(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Old")
        svc = SessionService(sm)
        result = svc.rename_session("desktop:chat1", "  Trimmed Title  ")
        assert result["title"] == "Trimmed Title"


class TestSessionServiceArchive:

    def test_archive_marks_session(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Active Chat")
        svc = SessionService(sm)
        result = svc.archive_session("desktop:chat1")
        assert result["key"] == "desktop:chat1"
        assert result["archived"] is True
        # Verify persisted
        sm.invalidate("desktop:chat1")
        session = sm.get_or_create("desktop:chat1")
        assert session.metadata.get("archived") is True

    def test_archived_hidden_from_list_by_default(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:active", title="Active")
        _seed_session(sm, "desktop:archived", title="Old")
        svc = SessionService(sm)
        svc.archive_session("desktop:archived")
        result = svc.list_sessions()
        assert result["count"] == 1
        assert result["sessions"][0]["key"] == "desktop:active"

    def test_archived_visible_with_include_archived(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:active", title="Active")
        _seed_session(sm, "desktop:archived", title="Old")
        svc = SessionService(sm)
        svc.archive_session("desktop:archived")
        result = svc.list_sessions(include_archived=True)
        assert result["count"] == 2
        # Archived item should have archived flag
        archived_items = [s for s in result["sessions"] if s.get("archived")]
        assert len(archived_items) == 1

    def test_unarchive_restores_session(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Chat")
        svc = SessionService(sm)
        svc.archive_session("desktop:chat1")
        result = svc.unarchive_session("desktop:chat1")
        assert result["archived"] is False
        # Should appear in list again
        listed = svc.list_sessions()
        assert listed["count"] == 1

    def test_archive_empty_key_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="key must not be empty"):
            svc.archive_session("")

    def test_unarchive_empty_key_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="key must not be empty"):
            svc.unarchive_session("")

    def test_archived_hidden_from_search_by_default(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:active", title="Quantum Physics")
        _seed_session(sm, "desktop:archived", title="Quantum Chemistry")
        svc = SessionService(sm)
        svc.archive_session("desktop:archived")
        result = svc.search_sessions("Quantum")
        assert result["count"] == 1
        assert result["sessions"][0]["key"] == "desktop:active"

    def test_archived_visible_in_search_with_include_archived(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:active", title="Quantum Physics")
        _seed_session(sm, "desktop:archived", title="Quantum Chemistry")
        svc = SessionService(sm)
        svc.archive_session("desktop:archived")
        result = svc.search_sessions("Quantum", include_archived=True)
        assert result["count"] == 2

    def test_archive_persists_to_disk(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Test")
        svc = SessionService(sm)
        svc.archive_session("desktop:chat1")
        # Verify on disk via fresh SessionManager
        sm2 = SessionManager(tmp_path)
        session = sm2.get_or_create("desktop:chat1")
        assert session.metadata.get("archived") is True


class TestSessionServiceDelete:

    def test_delete_existing(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [{"role": "user", "content": "hi"}])
        svc = SessionService(sm)
        result = svc.delete_session("desktop:chat1")
        assert result["key"] == "desktop:chat1"
        assert result["deleted"] is True

    def test_delete_nonexistent(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        result = svc.delete_session("desktop:nope")
        assert result["key"] == "desktop:nope"
        assert result["deleted"] is False

    def test_delete_empty_key_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="key must not be empty"):
            svc.delete_session("")


class TestSessionServiceSearch:

    def test_search_by_title(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:physics", title="Physics Homework")
        _seed_session(sm, "desktop:cooking", title="Cooking Recipe")
        svc = SessionService(sm)
        result = svc.search_sessions("Physics")
        assert result["count"] == 1
        assert result["sessions"][0]["key"] == "desktop:physics"
        assert result["query"] == "Physics"

    def test_search_by_content(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [
            {"role": "user", "content": "Tell me about quantum mechanics"},
        ])
        _seed_session(sm, "desktop:chat2", [
            {"role": "user", "content": "How to bake bread"},
        ])
        svc = SessionService(sm)
        result = svc.search_sessions("quantum")
        assert result["count"] == 1
        assert result["sessions"][0]["key"] == "desktop:chat1"

    def test_search_case_insensitive(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="PYTHON Programming")
        svc = SessionService(sm)
        result = svc.search_sessions("python")
        assert result["count"] == 1

    def test_search_empty_query(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Some Session")
        svc = SessionService(sm)
        result = svc.search_sessions("")
        assert result["count"] == 0
        assert result["sessions"] == []

    def test_search_no_results(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="Hello")
        svc = SessionService(sm)
        result = svc.search_sessions("nonexistent query xyz")
        assert result["count"] == 0

    def test_search_no_false_positive_on_json_keys(self, tmp_path: Path):
        """Searching for 'role' or 'metadata' should not match every session."""
        sm = SessionManager(tmp_path)
        # Create a session that does NOT contain the word "role" in its content
        _seed_session(sm, "desktop:chat1", [
            {"role": "user", "content": "Tell me about the weather"},
        ])
        _seed_session(sm, "desktop:chat2", [
            {"role": "assistant", "content": "The weather is sunny"},
        ])
        svc = SessionService(sm)
        # "role" appears as a JSON key in every message line, but should not match
        result = svc.search_sessions("role")
        assert result["count"] == 0
        # "metadata" appears in the metadata line of every session, but should not match
        result2 = svc.search_sessions("metadata")
        assert result2["count"] == 0

    def test_search_matches_real_content_not_json_structure(self, tmp_path: Path):
        """Real content matches work, but structural JSON words don't."""
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [
            {"role": "user", "content": "What is the weather like?"},
        ])
        svc = SessionService(sm)
        # Real word from content should match
        result = svc.search_sessions("weather")
        assert result["count"] == 1
        assert result["sessions"][0]["key"] == "desktop:chat1"

    def test_search_does_not_match_tool_call_id(self, tmp_path: Path):
        """tool_call_id values should not be searched."""
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [
            {"role": "assistant", "content": "done", "tool_call_id": "call_abc123xyz"},
        ])
        svc = SessionService(sm)
        result = svc.search_sessions("abc123xyz")
        assert result["count"] == 0

    def test_search_sorted_by_updated_at_descending(self, tmp_path: Path):
        """Search results must be sorted by real session.updated_at, not stale metadata."""
        import time
        sm = SessionManager(tmp_path)
        # Create old session with a message containing "quantum"
        _seed_session(sm, "desktop:old", [
            {"role": "user", "content": "Tell me about quantum mechanics"},
        ])
        time.sleep(0.05)
        # Create new session also containing "quantum"
        _seed_session(sm, "desktop:new", [
            {"role": "user", "content": "quantum computing basics"},
        ])
        # Now append a newer message to old, making its real updated_at later
        time.sleep(0.05)
        session = sm.get_or_create("desktop:old")
        session.add_message("user", "more quantum questions")
        sm.save(session)

        svc = SessionService(sm)
        result = svc.search_sessions("quantum")
        assert result["count"] == 2
        # old should be first because it was updated more recently
        assert result["sessions"][0]["key"] == "desktop:old"
        assert result["sessions"][1]["key"] == "desktop:new"


class TestSessionServiceLoad:

    def test_load_with_messages(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there", "tool_calls": [{"id": "tc1", "function": {"name": "exec"}}], "tool_call_id": "tc1"},
        ])
        svc = SessionService(sm)
        result = svc.load_session("desktop:chat1")
        assert result["key"] == "desktop:chat1"
        assert result["message_count"] == 2
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"

    def test_load_sanitizes_internal_fields(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", [
            {"role": "assistant", "content": "thinking", "reasoning_content": "internal thought"},
            {"role": "tool", "content": "result", "tool_call_id": "tc1", "_internal": True},
        ])
        svc = SessionService(sm)
        result = svc.load_session("desktop:chat1")
        for msg in result["messages"]:
            assert "reasoning_content" not in msg
            assert "_internal" not in msg
        # Allowed keys are present
        allowed = {"role", "content", "tool_calls", "tool_call_id", "name", "timestamp"}
        for msg in result["messages"]:
            for key in msg:
                assert key in allowed, f"Unexpected key '{key}' in sanitized message"

    def test_load_empty_session(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:empty", [])
        svc = SessionService(sm)
        result = svc.load_session("desktop:empty")
        assert result["message_count"] == 0
        assert result["messages"] == []

    def test_load_nonexistent_session(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        # Loading a non-existent key creates an empty session (get_or_create)
        result = svc.load_session("desktop:nonexistent")
        assert result["key"] == "desktop:nonexistent"
        assert result["message_count"] == 0

    def test_load_empty_key_raises(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        with pytest.raises(ValueError, match="key must not be empty"):
            svc.load_session("")

    def test_load_returns_title(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        _seed_session(sm, "desktop:chat1", title="My Title")
        svc = SessionService(sm)
        result = svc.load_session("desktop:chat1")
        assert result["title"] == "My Title"


class TestJsonLCompatibility:
    """Verify SessionService doesn't break the JSONL storage format."""

    def test_session_still_jsonl_after_service_operations(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)

        # Create via service
        svc.create_session("desktop:compat", title="Compat Test")

        # Add messages via SessionManager directly (simulating CLI/gateway)
        session = sm.get_or_create("desktop:compat")
        session.add_message("user", "hello from CLI")
        sm.save(session)

        # Load via service
        result = svc.load_session("desktop:compat")
        assert result["message_count"] == 1
        assert result["messages"][0]["content"] == "hello from CLI"

        # Verify raw file is still valid JSONL
        path = sm._get_session_path("desktop:compat")
        assert path.exists()
        with open(path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        # First line is metadata, rest are messages
        assert len(lines) >= 2
        meta = json.loads(lines[0])
        assert meta["_type"] == "metadata"
        assert meta["metadata"]["title"] == "Compat Test"

    def test_sm_after_rename_still_jsonl(self, tmp_path: Path):
        sm = SessionManager(tmp_path)
        svc = SessionService(sm)
        _seed_session(sm, "desktop:chat1", [{"role": "user", "content": "hi"}], title="Old")
        svc.rename_session("desktop:chat1", "New Title")

        # Reload with a fresh SessionManager to ensure disk format is correct
        sm2 = SessionManager(tmp_path)
        session = sm2.get_or_create("desktop:chat1")
        assert session.metadata["title"] == "New Title"
        assert len(session.messages) == 1


# ══════════════════════════════════════════════════════════════════════════
# IPC handler tests via RpcDispatcher
# ══════════════════════════════════════════════════════════════════════════

class TestSessionIpcHandlers:

    @pytest.mark.asyncio
    async def test_session_list_empty(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=1, method="session.list")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 0
        assert resp.result["sessions"] == []

    @pytest.mark.asyncio
    async def test_session_list_with_data(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", [
            {"role": "user", "content": "hello"},
        ], title="Test Session")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=2, method="session.list")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 1
        item = resp.result["sessions"][0]
        assert item["key"] == "desktop:chat1"

    @pytest.mark.asyncio
    async def test_session_create(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=3, method="session.create", params={"key": "desktop:new", "title": "New Chat"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["key"] == "desktop:new"
        assert resp.result["title"] == "New Chat"

    @pytest.mark.asyncio
    async def test_session_create_missing_key(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=4, method="session.create", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None
        assert resp.error.code == ERROR_INTERNAL  # ValueError caught as internal

    @pytest.mark.asyncio
    async def test_session_rename(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", title="Old")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=5, method="session.rename", params={"key": "desktop:chat1", "title": "New Title"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_session_rename_missing_title(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=6, method="session.rename", params={"key": "desktop:chat1"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_session_delete(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", [])

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=7, method="session.delete", params={"key": "desktop:chat1"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["deleted"] is True

    @pytest.mark.asyncio
    async def test_session_delete_nonexistent(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=8, method="session.delete", params={"key": "desktop:nope"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["deleted"] is False

    @pytest.mark.asyncio
    async def test_session_search(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:physics", title="Physics Chat")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=9, method="session.search", params={"query": "Physics"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 1
        assert resp.result["query"] == "Physics"

    @pytest.mark.asyncio
    async def test_session_search_empty_query(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=10, method="session.search", params={"query": ""})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 0

    @pytest.mark.asyncio
    async def test_session_load(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ], title="Test Chat")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=11, method="session.load", params={"key": "desktop:chat1"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["key"] == "desktop:chat1"
        assert resp.result["message_count"] == 2
        assert len(resp.result["messages"]) == 2

    @pytest.mark.asyncio
    async def test_session_load_missing_key(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=12, method="session.load", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_session_methods_registered(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        names = dispatcher.method_names
        assert "session.list" in names
        assert "session.create" in names
        assert "session.rename" in names
        assert "session.archive" in names
        assert "session.unarchive" in names
        assert "session.delete" in names
        assert "session.search" in names
        assert "session.load" in names

    @pytest.mark.asyncio
    async def test_session_archive(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", title="Test")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=14, method="session.archive", params={"key": "desktop:chat1"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["archived"] is True

    @pytest.mark.asyncio
    async def test_session_archive_hides_from_list(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", title="Test")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        # Archive it
        archive_req = JsonRpcRequest(id=15, method="session.archive", params={"key": "desktop:chat1"})
        await dispatcher.dispatch(archive_req)
        # List should not include archived
        list_req = JsonRpcRequest(id=16, method="session.list")
        list_resp = await dispatcher.dispatch(list_req)
        assert list_resp.result["count"] == 0
        # List with include_archived should include it
        list_req2 = JsonRpcRequest(id=17, method="session.list", params={"include_archived": True})
        list_resp2 = await dispatcher.dispatch(list_req2)
        assert list_resp2.result["count"] == 1

    @pytest.mark.asyncio
    async def test_session_unarchive(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", title="Test")

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        # Archive then unarchive
        archive_req = JsonRpcRequest(id=18, method="session.archive", params={"key": "desktop:chat1"})
        await dispatcher.dispatch(archive_req)
        unarchive_req = JsonRpcRequest(id=19, method="session.unarchive", params={"key": "desktop:chat1"})
        resp = await dispatcher.dispatch(unarchive_req)
        assert resp.error is None
        assert resp.result["archived"] is False
        # Should appear in list again
        list_req = JsonRpcRequest(id=20, method="session.list")
        list_resp = await dispatcher.dispatch(list_req)
        assert list_resp.result["count"] == 1

    @pytest.mark.asyncio
    async def test_session_archive_missing_key(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=21, method="session.archive", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_session_load_sanitizes_messages(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        _seed_session(rt.session_manager, "desktop:chat1", [
            {"role": "assistant", "content": "thinking", "reasoning_content": "secret thought"},
        ])

        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(id=13, method="session.load", params={"key": "desktop:chat1"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        for msg in resp.result["messages"]:
            assert "reasoning_content" not in msg
