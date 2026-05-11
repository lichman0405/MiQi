"""Tests for newly added RPC handlers: config.write, config.testProvider,
mcp.status, cron.*, heartbeat.*, chat.regenerate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from miqi.config.schema import Config
from miqi.ipc.protocol import JsonRpcRequest
from miqi.providers.base import LLMProvider, LLMResponse
from miqi.runtime.factory import Runtime, create_runtime, wire_cron_callback


# ── Fake provider for testing ────────────────────────────────────────────

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
# config.write
# ══════════════════════════════════════════════════════════════════════════

class TestConfigWrite:

    @pytest.mark.asyncio
    async def test_config_write_updates_config(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=1, method="config.write",
            params={"updates": {"agents": {"defaults": {"name": "test-agent"}}}},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is True
        assert rt.config.agents.defaults.name == "test-agent"

    @pytest.mark.asyncio
    async def test_config_write_accepts_desktop_nested_settings_payload(self, tmp_path: Path, monkeypatch):
        """Desktop Settings must send nested updates, including tools.restrict_to_workspace."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=11,
            method="config.write",
            params={
                "updates": {
                    "agents": {
                        "defaults": {
                            "model": "openai/gpt-4o",
                            "name": "desktop-agent",
                            "workspace": str(tmp_path / "project"),
                            "max_tokens": 4096,
                            "temperature": 0.7,
                        }
                    },
                    "providers": {
                        "openai": {
                            "api_key": "sk-desktop-test",
                            "api_base": "https://api.example.test/v1",
                        }
                    },
                    "tools": {
                        "restrict_to_workspace": True,
                    },
                }
            },
        )

        resp = await dispatcher.dispatch(req)

        assert resp.error is None
        assert resp.result["success"] is True
        assert rt.config.agents.defaults.model == "openai/gpt-4o"
        assert rt.config.agents.defaults.name == "desktop-agent"
        assert rt.config.agents.defaults.workspace == str(tmp_path / "project")
        assert rt.config.agents.defaults.max_tokens == 4096
        assert rt.config.agents.defaults.temperature == 0.7
        assert rt.config.providers.openai.api_key == "sk-desktop-test"
        assert rt.config.providers.openai.api_base == "https://api.example.test/v1"
        assert rt.config.tools.restrict_to_workspace is True

    @pytest.mark.asyncio
    async def test_config_write_rejects_invalid_updates(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=2, method="config.write",
            params={"updates": {"agents": {"defaults": {"max_tokens": "not_a_number"}}}},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None
        assert "Invalid config" in resp.error.message

    @pytest.mark.asyncio
    async def test_config_write_requires_updates(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=3, method="config.write", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_config_write_persists_to_disk(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(config_path))
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=4, method="config.write",
            params={"updates": {"agents": {"defaults": {"name": "persisted-agent"}}}},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        # Verify the file was written
        assert config_path.exists()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["agents"]["defaults"]["name"] == "persisted-agent"

    @pytest.mark.asyncio
    async def test_config_write_rejects_unknown_key(self, tmp_path: Path, monkeypatch):
        """Unknown config keys must not silently succeed."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=5, method="config.write",
            params={"updates": {"agents": {"defaults": {"definitely_unknown_key": "value"}}}},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None
        assert "Unknown config key" in resp.error.message

    @pytest.mark.asyncio
    async def test_config_write_rejects_top_level_unknown_key(self, tmp_path: Path, monkeypatch):
        """Unknown top-level config keys must not silently succeed."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=6, method="config.write",
            params={"updates": {"totally_unknown_section": {"foo": "bar"}}},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None
        assert "Unknown config key" in resp.error.message


# ══════════════════════════════════════════════════════════════════════════
# config.testProvider
# ══════════════════════════════════════════════════════════════════════════

class TestConfigTestProvider:

    @pytest.mark.asyncio
    async def test_test_provider_returns_success(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=1, method="config.testProvider",
            params={"provider": "openai", "model": "openai/gpt-4o"},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        # Our FakeProvider returns success
        assert resp.result["success"] is True

    @pytest.mark.asyncio
    async def test_test_provider_requires_provider_name(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=2, method="config.testProvider", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_test_provider_returns_error_on_failure(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        # Make provider.chat raise
        rt.provider.chat = AsyncMock(side_effect=RuntimeError("connection failed"))
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=3, method="config.testProvider",
            params={"provider": "openai"},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None  # Not an RPC error — returns error in result
        assert resp.result["success"] is False
        assert "connection failed" in resp.result["error"]


# ══════════════════════════════════════════════════════════════════════════
# mcp.status
# ══════════════════════════════════════════════════════════════════════════

class TestMcpStatus:

    @pytest.mark.asyncio
    async def test_mcp_status_no_servers(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=1, method="mcp.status")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["connected"] is False
        assert resp.result["servers"] == {}

    @pytest.mark.asyncio
    async def test_mcp_status_with_configured_servers(self, tmp_path: Path, monkeypatch):
        from miqi.config.schema import MCPServerConfig

        rt = _make_runtime(tmp_path, monkeypatch)
        rt.config.tools.mcp_servers["test-mcp"] = MCPServerConfig(command="echo")
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=2, method="mcp.status")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "test-mcp" in resp.result["servers"]
        assert resp.result["servers"]["test-mcp"]["configured"] is True


# ══════════════════════════════════════════════════════════════════════════
# state-change events
# ══════════════════════════════════════════════════════════════════════════

class TestStateChangeEvents:

    @pytest.mark.asyncio
    async def test_memory_update_emits_memory_changed(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        from miqi.events.models import MemoryChanged

        collected = []

        async def _collect(event):
            collected.append(event)

        rt.events.subscribe(_collect)
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=1,
            method="memory.update",
            params={"text": "remembered from desktop", "action": "remember"},
        )
        resp = await dispatcher.dispatch(req)

        assert resp.error is None
        assert any(
            isinstance(event, MemoryChanged) and event.action == "snapshot"
            for event in collected
        )

    @pytest.mark.asyncio
    async def test_memory_lesson_mutation_emits_memory_changed(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        from miqi.events.models import MemoryChanged

        collected = []

        async def _collect(event):
            collected.append(event)

        rt.events.subscribe(_collect)
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=2,
            method="memory.learnLesson",
            params={"trigger": "slow query", "better_action": "add an index"},
        )
        resp = await dispatcher.dispatch(req)

        assert resp.error is None
        assert any(
            isinstance(event, MemoryChanged) and event.action == "lesson"
            for event in collected
        )

    @pytest.mark.asyncio
    async def test_cron_mutations_emit_cron_job_changed(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        from miqi.events.models import CronJobChanged

        collected = []

        async def _collect(event):
            collected.append(event)

        rt.events.subscribe(_collect)
        dispatcher = RpcDispatcher(rt)

        add_resp = await dispatcher.dispatch(JsonRpcRequest(
            id=10,
            method="cron.add",
            params={
                "name": "desktop-job",
                "message": "hello from desktop",
                "schedule": {"kind": "every", "every_ms": 60000},
            },
        ))
        assert add_resp.error is None
        job_id = add_resp.result["job_id"]

        update_resp = await dispatcher.dispatch(JsonRpcRequest(
            id=11,
            method="cron.update",
            params={"job_id": job_id, "enabled": False},
        ))
        assert update_resp.error is None

        delete_resp = await dispatcher.dispatch(JsonRpcRequest(
            id=12,
            method="cron.delete",
            params={"job_id": job_id},
        ))
        assert delete_resp.error is None
        assert delete_resp.result["success"] is True

        cron_events = [
            event for event in collected
            if isinstance(event, CronJobChanged)
        ]
        assert [event.action for event in cron_events] == ["added", "updated", "deleted"]
        assert all(event.job_id == job_id for event in cron_events)


# ══════════════════════════════════════════════════════════════════════════
# cron.*
# ══════════════════════════════════════════════════════════════════════════

class TestCronHandlers:

    @pytest.mark.asyncio
    async def test_cron_list_empty(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=1, method="cron.list")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["count"] == 0
        assert resp.result["jobs"] == []

    @pytest.mark.asyncio
    async def test_cron_add_and_list(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Add a job
        req = JsonRpcRequest(
            id=10, method="cron.add",
            params={
                "name": "test-job",
                "message": "hello from cron",
                "schedule": {"kind": "every", "every_ms": 60000},
            },
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is True
        job_id = resp.result["job_id"]

        # List should include it
        req2 = JsonRpcRequest(id=11, method="cron.list", params={"include_disabled": True})
        resp2 = await dispatcher.dispatch(req2)
        assert resp2.error is None
        assert resp2.result["count"] == 1
        assert resp2.result["jobs"][0]["id"] == job_id
        assert resp2.result["jobs"][0]["name"] == "test-job"

    @pytest.mark.asyncio
    async def test_cron_add_requires_name(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=20, method="cron.add", params={"message": "x"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_cron_add_requires_message(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=21, method="cron.add", params={"name": "x"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_cron_update_enables_disables(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Add a job first
        add_req = JsonRpcRequest(
            id=30, method="cron.add",
            params={"name": "toggle-job", "message": "test", "schedule": {"kind": "every", "every_ms": 60000}},
        )
        add_resp = await dispatcher.dispatch(add_req)
        job_id = add_resp.result["job_id"]

        # Disable it
        req = JsonRpcRequest(id=31, method="cron.update", params={"job_id": job_id, "enabled": False})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is True

        # Verify in list (include_disabled)
        list_req = JsonRpcRequest(id=32, method="cron.list", params={"include_disabled": True})
        list_resp = await dispatcher.dispatch(list_req)
        job = [j for j in list_resp.result["jobs"] if j["id"] == job_id][0]
        assert job["enabled"] is False

    @pytest.mark.asyncio
    async def test_cron_update_requires_job_id(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=33, method="cron.update", params={"enabled": True})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None

    @pytest.mark.asyncio
    async def test_cron_delete(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Add a job
        add_req = JsonRpcRequest(
            id=40, method="cron.add",
            params={"name": "del-job", "message": "test", "schedule": {"kind": "every", "every_ms": 60000}},
        )
        add_resp = await dispatcher.dispatch(add_req)
        job_id = add_resp.result["job_id"]

        # Delete it
        req = JsonRpcRequest(id=41, method="cron.delete", params={"job_id": job_id})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is True

        # Verify gone
        list_req = JsonRpcRequest(id=42, method="cron.list", params={"include_disabled": True})
        list_resp = await dispatcher.dispatch(list_req)
        assert list_resp.result["count"] == 0

    @pytest.mark.asyncio
    async def test_cron_delete_nonexistent(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=43, method="cron.delete", params={"job_id": "no-such-job"})
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is False

    @pytest.mark.asyncio
    async def test_cron_delete_requires_job_id(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=44, method="cron.delete", params={})
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None


# ══════════════════════════════════════════════════════════════════════════
# heartbeat.*
# ══════════════════════════════════════════════════════════════════════════

class TestHeartbeatHandlers:

    @pytest.mark.asyncio
    async def test_heartbeat_status(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=1, method="heartbeat.status")
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "enabled" in resp.result
        assert "interval_seconds" in resp.result
        assert "running" in resp.result
        assert resp.result["running"] is False

    @pytest.mark.asyncio
    async def test_heartbeat_update_enabled(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(config_path))
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=2, method="heartbeat.update",
            params={"enabled": False},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is True
        assert rt.config.heartbeat.enabled is False

    @pytest.mark.asyncio
    async def test_heartbeat_update_interval(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(config_path))
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=3, method="heartbeat.update",
            params={"interval_seconds": 600},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is True
        assert rt.config.heartbeat.interval_seconds == 600

    @pytest.mark.asyncio
    async def test_heartbeat_update_syncs_with_service(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(config_path))
        rt = _make_runtime(tmp_path, monkeypatch)
        hb = rt.heartbeat_service
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=4, method="heartbeat.update",
            params={"enabled": True, "interval_seconds": 900},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert hb.enabled is True
        assert hb.interval_s == 900


# ══════════════════════════════════════════════════════════════════════════
# chat.regenerate
# ══════════════════════════════════════════════════════════════════════════

class TestChatRegenerate:

    @pytest.mark.asyncio
    async def test_regenerate_with_session_history(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Set up session with user + assistant messages
        session_key = "desktop:default"
        session = rt.session_manager.get_or_create(session_key)
        session.add_message("user", "Hello")
        session.add_message("assistant", "Hi there!")
        rt.session_manager.save(session)

        rt.agent.process_direct = AsyncMock(return_value="Regenerated!")

        req = JsonRpcRequest(
            id=1, method="chat.regenerate",
            params={"session_key": session_key},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert "execution_id" in resp.result

    @pytest.mark.asyncio
    async def test_regenerate_no_user_message(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Empty session
        session_key = "desktop:empty"
        rt.session_manager.get_or_create(session_key)

        req = JsonRpcRequest(
            id=2, method="chat.regenerate",
            params={"session_key": session_key},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is not None
        assert "No user message" in resp.error.message

    @pytest.mark.asyncio
    async def test_regenerate_removes_trailing_assistant_messages(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        session_key = "desktop:regen"
        session = rt.session_manager.get_or_create(session_key)
        session.add_message("user", "Question 1")
        session.add_message("assistant", "Answer 1")
        session.add_message("user", "Question 2")
        session.add_message("assistant", "Answer 2")
        rt.session_manager.save(session)

        rt.agent.process_direct = AsyncMock(return_value="New answer")

        req = JsonRpcRequest(
            id=3, method="chat.regenerate",
            params={"session_key": session_key},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None

        # Verify trailing assistant messages were removed before re-send
        session_after = rt.session_manager.get_or_create(session_key)
        # After regeneration, the agent sends a new message, but the
        # original "Answer 2" should have been popped.
        user_msgs = [m for m in session_after.messages if m.get("role") == "user"]
        assert len(user_msgs) == 2


# ══════════════════════════════════════════════════════════════════════════
# Method registration
# ══════════════════════════════════════════════════════════════════════════

class TestNewMethodRegistration:

    @pytest.mark.asyncio
    async def test_all_new_methods_registered(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        names = dispatcher.method_names
        for method in [
            "config.write",
            "config.testProvider",
            "mcp.status",
            "cron.list",
            "cron.add",
            "cron.update",
            "cron.delete",
            "heartbeat.status",
            "heartbeat.update",
            "chat.regenerate",
            "session.archive",
            "session.unarchive",
        ]:
            assert method in names, f"{method} not in registered methods"


# ══════════════════════════════════════════════════════════════════════════
# config.testProvider edge cases
# ══════════════════════════════════════════════════════════════════════════

class TestConfigTestProviderEdgeCases:

    @pytest.mark.asyncio
    async def test_test_provider_uses_runtime_provider(self, tmp_path: Path, monkeypatch):
        """Without api_key/api_base, testProvider uses the runtime's current provider."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=1, method="config.testProvider",
            params={"provider": "openai", "model": "fake-model"},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        # FakeProvider returns success
        assert resp.result["success"] is True

    @pytest.mark.asyncio
    async def test_test_provider_with_explicit_api_key(self, tmp_path: Path, monkeypatch):
        """With api_key, testProvider builds a temporary provider — no real network."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from unittest.mock import patch
        from miqi.ipc.handlers import RpcDispatcher

        # Patch OpenAIProvider at its source module so no real HTTP call is made
        mock_provider = AsyncMock()
        mock_provider.chat.return_value = LLMResponse(content="temp-ok")

        with patch("miqi.providers.openai_provider.OpenAIProvider", return_value=mock_provider) as mock_cls:
            req = JsonRpcRequest(
                id=2, method="config.testProvider",
                params={
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "sk-test-key-123",
                },
            )
            dispatcher = RpcDispatcher(rt)
            resp = await dispatcher.dispatch(req)

        assert resp.error is None
        assert resp.result["success"] is True
        # Verify the temporary provider was constructed with the explicit api_key
        mock_cls.assert_called_once()
        call_kwargs = mock_cls.call_args
        assert call_kwargs.kwargs.get("api_key") == "sk-test-key-123" or call_kwargs[1].get("api_key") == "sk-test-key-123"
        # Verify it was called .chat(), not the runtime's FakeProvider
        mock_provider.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_test_provider_with_explicit_api_base(self, tmp_path: Path, monkeypatch):
        """With api_base, testProvider builds a temporary provider."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from unittest.mock import patch
        from miqi.ipc.handlers import RpcDispatcher

        mock_provider = AsyncMock()
        mock_provider.chat.return_value = LLMResponse(content="temp-ok")

        with patch("miqi.providers.openai_provider.OpenAIProvider", return_value=mock_provider) as mock_cls:
            req = JsonRpcRequest(
                id=3, method="config.testProvider",
                params={
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "sk-key",
                    "api_base": "https://custom.example.com/v1",
                },
            )
            dispatcher = RpcDispatcher(rt)
            resp = await dispatcher.dispatch(req)

        assert resp.error is None
        assert resp.result["success"] is True
        call_kwargs = mock_cls.call_args
        assert call_kwargs.kwargs.get("api_base") == "https://custom.example.com/v1" or call_kwargs[1].get("api_base") == "https://custom.example.com/v1"

    @pytest.mark.asyncio
    async def test_test_provider_explicit_unknown_provider(self, tmp_path: Path, monkeypatch):
        """With api_key but unknown provider name, returns error in result."""
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=4, method="config.testProvider",
            params={"provider": "nonexistent_xyz", "model": "fake/test", "api_key": "sk-test-key"},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None  # Not an RPC error — returns error in result
        assert resp.result["success"] is False
        assert "Unknown provider" in resp.result["error"]

    @pytest.mark.asyncio
    async def test_test_provider_runtime_provider_failure(self, tmp_path: Path, monkeypatch):
        """When runtime provider's chat raises, returns error in result."""
        rt = _make_runtime(tmp_path, monkeypatch)
        rt.provider.chat = AsyncMock(side_effect=RuntimeError("connection failed"))
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(
            id=5, method="config.testProvider",
            params={"provider": "openai"},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["success"] is False
        assert "connection failed" in resp.result["error"]


# ══════════════════════════════════════════════════════════════════════════
# chat.approve choice variants
# ══════════════════════════════════════════════════════════════════════════

class TestChatApproveChoices:

    @pytest.mark.asyncio
    async def test_approve_with_session_choice(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
        monkeypatch.setenv("MIQI_DATA_DIR", str(tmp_path / "data"))
        from miqi.runtime.factory import create_runtime, wire_cron_callback
        rt = create_runtime(
            Config(),
            make_provider=_make_fake_provider,
            init_session_manager=True,
            enable_desktop_approval=True,
        )
        wire_cron_callback(rt)

        from miqi.ipc.handlers import RpcDispatcher
        from miqi.events.models import ApprovalRequested
        from miqi.runtime.approval import ApprovalDecision

        svc = rt.approval_service
        collected = []
        rt.events.subscribe(lambda e: collected.append(e))

        async def _request():
            return await svc.request_approval(
                execution_id="e1", tool_name="exec", tool_call_id="tc1",
                session_key="desktop:s1",
                pattern_description="recursive delete",
                command="rm -rf /tmp/test",
            )

        task = asyncio.create_task(_request())
        await asyncio.sleep(0.05)

        approval_id = [e for e in collected if isinstance(e, ApprovalRequested)][0].approval_id

        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(
            id=1, method="chat.approve",
            params={"approval_id": approval_id, "choice": "session"},
        )
        resp = await dispatcher.dispatch(req)
        assert resp.error is None
        assert resp.result["decision"] == "session"
        assert resp.result["success"] is True

        decision = await task
        assert decision == ApprovalDecision.SESSION

    @pytest.mark.asyncio
    async def test_approve_with_always_choice(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        monkeypatch.setenv("MIQI_CONFIG_PATH", str(tmp_path / "config.json"))
        monkeypatch.setenv("MIQI_DATA_DIR", str(tmp_path / "data"))
        from unittest.mock import patch
        from miqi.runtime.factory import create_runtime, wire_cron_callback
        rt = create_runtime(
            Config(),
            make_provider=_make_fake_provider,
            init_session_manager=True,
            enable_desktop_approval=True,
        )
        wire_cron_callback(rt)

        from miqi.ipc.handlers import RpcDispatcher
        from miqi.events.models import ApprovalRequested
        from miqi.runtime.approval import ApprovalDecision

        svc = rt.approval_service
        collected = []
        rt.events.subscribe(lambda e: collected.append(e))

        async def _request():
            return await svc.request_approval(
                execution_id="e1", tool_name="exec", tool_call_id="tc1",
                session_key="desktop:s1",
                pattern_description="recursive delete",
                command="rm -rf /tmp/test",
            )

        task = asyncio.create_task(_request())
        await asyncio.sleep(0.05)

        approval_id = [e for e in collected if isinstance(e, ApprovalRequested)][0].approval_id

        dispatcher = RpcDispatcher(rt)
        try:
            with patch("miqi.agent.command_approval._save_permanent_allowlist"):
                req = JsonRpcRequest(
                    id=2, method="chat.approve",
                    params={"approval_id": approval_id, "choice": "always"},
                )
                resp = await dispatcher.dispatch(req)
            assert resp.error is None
            assert resp.result["decision"] == "always"
            assert resp.result["success"] is True

            decision = await task
            assert decision == ApprovalDecision.ALWAYS
        finally:
            from miqi.agent.command_approval import _lock, _permanent_approved, clear_session
            clear_session("desktop:s1")
            with _lock:
                _permanent_approved.discard("recursive delete")
