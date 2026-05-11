"""Tests for miqi.ipc — protocol, handlers, and transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from miqi.config.schema import Config, MCPServerConfig
from miqi.ipc.protocol import (
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_PARSE_ERROR,
    JsonRpcError,
    JsonRpcEvent,
    JsonRpcRequest,
    JsonRpcResponse,
    make_error_response,
    make_success_response,
)
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
# Protocol models
# ══════════════════════════════════════════════════════════════════════════

class TestProtocolModels:

    def test_request_parse(self):
        raw = {"jsonrpc": "2.0", "id": 1, "method": "app.status", "params": {}}
        req = JsonRpcRequest.model_validate(raw)
        assert req.id == 1
        assert req.method == "app.status"

    def test_request_params_default_empty(self):
        req = JsonRpcRequest(id=1, method="test")
        assert req.params == {}

    def test_response_success(self):
        resp = make_success_response(1, {"status": "ok"})
        assert resp.id == 1
        assert resp.result == {"status": "ok"}
        assert resp.error is None

    def test_response_error(self):
        resp = make_error_response(2, ERROR_METHOD_NOT_FOUND, "not found")
        assert resp.id == 2
        assert resp.error.code == ERROR_METHOD_NOT_FOUND
        assert resp.result is None

    def test_event_model(self):
        evt = JsonRpcEvent(method="RunStarted", params={"execution_id": "abc"})
        assert evt.method == "RunStarted"
        assert evt.params["execution_id"] == "abc"

    def test_error_with_data(self):
        resp = make_error_response(3, ERROR_INVALID_PARAMS, "bad", data={"field": "x"})
        assert resp.error.data == {"field": "x"}

    def test_response_serialize(self):
        resp = make_success_response(1, {"key": "val"})
        d = resp.model_dump(exclude_none=True)
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 1
        assert d["result"] == {"key": "val"}
        assert "error" not in d


# ══════════════════════════════════════════════════════════════════════════
# RpcDispatcher
# ══════════════════════════════════════════════════════════════════════════

class TestRpcDispatcher:

    @pytest.mark.asyncio
    async def test_app_status(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=1, method="app.status")
        resp = await dispatcher.dispatch(req)

        assert resp.id == 1
        assert resp.error is None
        assert resp.result["status"] == "running"
        assert resp.result["model"] == rt.config.agents.defaults.model
        assert resp.result["workspace"] == str(rt.config.workspace_path)

    @pytest.mark.asyncio
    async def test_config_read(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=2, method="config.read")
        resp = await dispatcher.dispatch(req)

        assert resp.id == 2
        assert resp.error is None
        assert "agents" in resp.result
        assert "providers" in resp.result

    @pytest.mark.asyncio
    async def test_config_read_redacts_sensitive_values(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        rt.config.providers.openrouter.api_key = "sk-or-secret"
        rt.config.providers.openrouter.extra_headers = {"APP-Code": "secret-code"}
        rt.config.tools.web.search.api_key = "brave-secret"
        rt.config.tools.web.fetch.ollama_api_key = "ollama-secret"
        rt.config.tools.papers.semantic_scholar_api_key = "paper-secret"
        rt.config.tools.mcp_servers["pdf2zh"] = MCPServerConfig(
            command="python",
            env={"OPENAI_API_KEY": "mcp-secret", "EMPTY": ""},
            headers={"Authorization": "Bearer token"},
        )

        from miqi.ipc.handlers import REDACTED_VALUE, RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=22, method="config.read")
        resp = await dispatcher.dispatch(req)

        assert resp.error is None
        assert resp.result["providers"]["openrouter"]["apiKey"] == REDACTED_VALUE
        assert resp.result["providers"]["openrouter"]["extraHeaders"] == {
            "APP-Code": REDACTED_VALUE,
        }
        assert resp.result["tools"]["web"]["search"]["apiKey"] == REDACTED_VALUE
        assert resp.result["tools"]["web"]["fetch"]["ollamaApiKey"] == REDACTED_VALUE
        assert resp.result["tools"]["papers"]["semanticScholarApiKey"] == REDACTED_VALUE
        server = resp.result["tools"]["mcpServers"]["pdf2zh"]
        assert server["env"] == {"OPENAI_API_KEY": REDACTED_VALUE, "EMPTY": ""}
        assert server["headers"] == {"Authorization": REDACTED_VALUE}

    @pytest.mark.asyncio
    async def test_tool_list(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=3, method="tool.list")
        resp = await dispatcher.dispatch(req)

        assert resp.id == 3
        assert resp.error is None
        assert "tools" in resp.result
        assert "count" in resp.result
        assert resp.result["count"] >= 0

    @pytest.mark.asyncio
    async def test_session_list(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=4, method="session.list")
        resp = await dispatcher.dispatch(req)

        assert resp.id == 4
        assert resp.error is None
        assert "sessions" in resp.result
        assert resp.result["count"] == 0

    @pytest.mark.asyncio
    async def test_method_not_found(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=99, method="nonexistent.method")
        resp = await dispatcher.dispatch(req)

        assert resp.id == 99
        assert resp.error is not None
        assert resp.error.code == ERROR_METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_handler_internal_error(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Inject a handler that raises to test the catch-all error path
        async def _boom(params):
            raise RuntimeError("boom")

        dispatcher._methods["test.boom"] = _boom

        req = JsonRpcRequest(id=5, method="test.boom", params={})
        resp = await dispatcher.dispatch(req)

        assert resp.id == 5
        assert resp.error is not None
        assert resp.error.code == ERROR_INTERNAL

    @pytest.mark.asyncio
    async def test_method_names(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        names = dispatcher.method_names
        assert "app.status" in names
        assert "config.read" in names
        assert "tool.list" in names
        assert "session.list" in names
        assert "chat.send" in names

    @pytest.mark.asyncio
    async def test_chat_send_returns_execution_id(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        rt.agent.process_direct = AsyncMock(return_value="hello back")

        req = JsonRpcRequest(id=10, method="chat.send", params={"message": "hello"})
        resp = await dispatcher.dispatch(req)

        assert resp.id == 10
        assert resp.error is None
        assert "execution_id" in resp.result
        assert len(resp.result["execution_id"]) == 12

    @pytest.mark.asyncio
    async def test_config_read_redacts_channel_credentials(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        rt.config.channels.feishu.app_id = "cli_test_app"
        rt.config.channels.feishu.app_secret = "super-secret"

        from miqi.ipc.handlers import REDACTED_VALUE, RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        req = JsonRpcRequest(id=30, method="config.read")
        resp = await dispatcher.dispatch(req)

        assert resp.error is None
        feishu = resp.result["channels"]["feishu"]
        assert feishu["appId"] == "cli_test_app"
        assert feishu["appSecret"] == REDACTED_VALUE


# ══════════════════════════════════════════════════════════════════════════
# Transport (_handle_line)
# ══════════════════════════════════════════════════════════════════════════

class TestTransportHandleLine:

    @pytest.mark.asyncio
    async def test_valid_request(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "app.status"})
        resp = await _handle_line(line, dispatcher)

        assert resp is not None
        assert resp.id == 1
        assert resp.result["status"] == "running"

    @pytest.mark.asyncio
    async def test_malformed_json(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        resp = await _handle_line("{not json}", dispatcher)
        assert resp is not None
        assert resp.error.code == ERROR_PARSE_ERROR

    @pytest.mark.asyncio
    async def test_invalid_request_missing_method(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        resp = await _handle_line(
            json.dumps({"jsonrpc": "2.0", "id": 2}),
            dispatcher,
        )
        assert resp is not None
        assert resp.error.code == ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        resp = await _handle_line(
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "bogus"}),
            dispatcher,
        )
        assert resp is not None
        assert resp.error.code == ERROR_METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_notification_returns_none(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # No id → notification, no response
        resp = await _handle_line(
            json.dumps({"jsonrpc": "2.0", "method": "app.status"}),
            dispatcher,
        )
        assert resp is None

    @pytest.mark.asyncio
    async def test_null_id_request_returns_response(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        resp = await _handle_line(
            json.dumps({"jsonrpc": "2.0", "id": None, "method": "app.status"}),
            dispatcher,
        )
        assert resp is not None
        assert resp.id is None
        assert resp.result["status"] == "running"

    @pytest.mark.asyncio
    async def test_null_id_error_response(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        resp = await _handle_line(
            json.dumps({"jsonrpc": "2.0", "id": None, "method": "nonexistent"}),
            dispatcher,
        )
        assert resp is not None
        assert resp.id is None
        assert resp.error.code == ERROR_METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_response_id_matches_request(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        for rid in [1, 42, "abc", "req-001"]:
            line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "app.status"})
            resp = await _handle_line(line, dispatcher)
            assert resp.id == rid, f"Expected id={rid!r}, got {resp.id!r}"

    @pytest.mark.asyncio
    async def test_invalid_request_preserves_id(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        resp = await _handle_line(
            json.dumps({"jsonrpc": "2.0", "id": "my-id", "foo": "bar"}),
            dispatcher,
        )
        assert resp is not None
        assert resp.id == "my-id"


# ══════════════════════════════════════════════════════════════════════════
# Transport (read_requests with mock reader)
# ══════════════════════════════════════════════════════════════════════════

class TestTransportReadStream:

    def test_write_response_outputs_json_line(self, capsys):
        from miqi.ipc.transport import _write_response

        _write_response(make_success_response(7, {"status": "ok"}))

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.endswith("\n")
        assert json.loads(captured.out) == {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"status": "ok"},
        }

    def test_write_response_preserves_null_id(self, capsys):
        from miqi.ipc.transport import _write_response

        _write_response(make_error_response(None, ERROR_PARSE_ERROR, "bad json"))

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["id"] is None
        assert payload["error"]["code"] == ERROR_PARSE_ERROR

    @pytest.mark.asyncio
    async def test_event_to_stdout_emits_method_style_notification(self, capsys):
        from miqi.events.models import RunStarted
        from miqi.ipc.transport import _event_to_stdout

        await _event_to_stdout(
            RunStarted(
                execution_id="exec-contract",
                session_key="desktop:default",
                preview="hello",
            )
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "RunStarted"
        assert payload["params"]["execution_id"] == "exec-contract"
        assert payload["params"]["session_key"] == "desktop:default"
        assert payload["params"]["preview"] == "hello"
        assert payload["params"]["channel"] == ""
        assert "type" not in payload["params"]

    @pytest.mark.asyncio
    async def test_read_multiple_requests(self, tmp_path: Path, monkeypatch):
        from miqi.ipc.transport import _handle_line
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Simulate multiple lines
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "app.status"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "config.read"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tool.list"}),
        ]
        for line in lines:
            resp = await _handle_line(line, dispatcher)
            assert resp is not None
            assert resp.error is None


class TestTransportE2E:
    """End-to-end read_requests → _write_response via mock reader + captured stdout."""

    @pytest.mark.asyncio
    async def test_e2e_multiple_requests_on_stdio(self, tmp_path: Path, monkeypatch, capsys):
        from miqi.ipc.transport import read_requests
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "app.status"}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "nonexistent"}) + "\n",
            "{bad json}\n",
        ]
        input_bytes = "".join(lines).encode("utf-8")

        reader = asyncio.StreamReader()
        reader.feed_data(input_bytes)
        reader.feed_eof()

        # Use read_requests with the mock reader
        await read_requests(dispatcher, reader=reader)

        captured = capsys.readouterr()
        output_lines = [l for l in captured.out.strip().split("\n") if l]

        # 3 requests → 3 response lines
        assert len(output_lines) == 3

        # Line 1: app.status success
        r1 = json.loads(output_lines[0])
        assert r1["id"] == 1
        assert r1["result"]["status"] == "running"

        # Line 2: unknown method error
        r2 = json.loads(output_lines[1])
        assert r2["id"] == 2
        assert r2["error"]["code"] == ERROR_METHOD_NOT_FOUND

        # Line 3: parse error (no id recoverable)
        r3 = json.loads(output_lines[2])
        assert r3["id"] is None
        assert r3["error"]["code"] == ERROR_PARSE_ERROR

    @pytest.mark.asyncio
    async def test_e2e_notification_produces_no_output(self, tmp_path: Path, monkeypatch, capsys):
        from miqi.ipc.transport import read_requests
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        # Notification: no "id" field
        input_bytes = (
            json.dumps({"jsonrpc": "2.0", "method": "app.status"}) + "\n"
        ).encode("utf-8")

        reader = asyncio.StreamReader()
        reader.feed_data(input_bytes)
        reader.feed_eof()

        await read_requests(dispatcher, reader=reader)

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    @pytest.mark.asyncio
    async def test_e2e_blank_lines_are_skipped(self, tmp_path: Path, monkeypatch, capsys):
        from miqi.ipc.transport import read_requests
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher
        dispatcher = RpcDispatcher(rt)

        input_bytes = (
            "\n\n" +
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "app.status"}) + "\n" +
            "\n"
        ).encode("utf-8")

        reader = asyncio.StreamReader()
        reader.feed_data(input_bytes)
        reader.feed_eof()

        await read_requests(dispatcher, reader=reader)

        captured = capsys.readouterr()
        output_lines = [l for l in captured.out.strip().split("\n") if l]
        assert len(output_lines) == 1
        assert json.loads(output_lines[0])["id"] == 1


class TestMemoryStatusContract:
    """Verify memory.status returns fields the desktop UI expects."""

    @pytest.mark.asyncio
    async def test_memory_status_shape_matches_frontend(self, tmp_path: Path, monkeypatch):
        rt = _make_runtime(tmp_path, monkeypatch)
        from miqi.ipc.handlers import RpcDispatcher

        dispatcher = RpcDispatcher(rt)
        req = JsonRpcRequest(jsonrpc="2.0", id=42, method="memory.status")
        resp = await dispatcher.dispatch(req)

        assert resp.id == 42
        assert resp.error is None
        result = resp.result

        # Fields consumed by desktop frontend (hooks.ts MemoryStatusResult)
        for field in ("ltm_items", "snapshot_exists", "lessons_count",
                      "self_improvement_enabled", "short_term_sessions",
                      "pending_sessions", "dirty_updates"):
            assert field in result, f"memory.status missing field: {field}"

        assert isinstance(result["ltm_items"], int)
        assert isinstance(result["snapshot_exists"], bool)
        assert isinstance(result["lessons_count"], int)
        assert isinstance(result["self_improvement_enabled"], bool)
