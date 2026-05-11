"""Tests for miqi.runtime.factory — shared runtime construction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from miqi.config.schema import Config
from miqi.providers.base import LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    """Minimal LLMProvider that returns canned responses."""

    def __init__(self, default_model: str = "fake-model"):
        super().__init__(api_key="test-key")
        self._default_model = default_model

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, *, on_delta=None):
        return LLMResponse(content="fake response")

    def get_default_model(self) -> str:
        return self._default_model


def _make_fake_provider(config: Config) -> FakeProvider:
    return FakeProvider()


class TestCreateRuntime:
    """Tests for create_runtime()."""

    def test_returns_runtime_with_all_components(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import Runtime, create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider)

        assert isinstance(rt, Runtime)
        assert rt.config is config
        assert rt.bus is not None
        assert isinstance(rt.provider, FakeProvider)
        assert rt.agent is not None
        assert rt.cron is not None
        assert rt.session_manager is None

    def test_session_manager_not_created_by_default(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider)
        assert rt.session_manager is None

    def test_session_manager_created_when_requested(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider, init_session_manager=True)
        assert rt.session_manager is not None

    def test_agent_loop_receives_correct_config(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider)

        agent = rt.agent
        assert agent.agent_name == config.agents.defaults.name
        assert agent.model == config.agents.defaults.model
        assert agent.temperature == config.agents.defaults.temperature
        assert agent.max_tokens == config.agents.defaults.max_tokens
        assert agent.workspace == config.workspace_path

    def test_agent_loop_receives_session_manager_when_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider, init_session_manager=True)
        assert rt.agent.sessions is rt.session_manager

    def test_cron_service_uses_config_timeout(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider)
        assert rt.cron is not None
        assert rt.cron.job_timeout == config.cron.job_timeout_seconds

    def test_provider_is_from_factory(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=_make_fake_provider)
        assert isinstance(rt.provider, FakeProvider)

    def test_custom_provider_factory(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        custom_provider = FakeProvider(default_model="custom-test")
        from miqi.runtime.factory import create_runtime

        rt = create_runtime(config, make_provider=lambda c: custom_provider)
        assert rt.provider is custom_provider


class TestWireCronCallback:
    """Tests for wire_cron_callback()."""

    def test_sets_cron_on_job(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime, wire_cron_callback

        rt = create_runtime(config, make_provider=_make_fake_provider)
        assert rt.cron.on_job is None

        wire_cron_callback(rt)
        assert rt.cron.on_job is not None
        assert callable(rt.cron.on_job)

    @pytest.mark.asyncio
    async def test_cron_callback_calls_process_direct(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime, wire_cron_callback

        rt = create_runtime(config, make_provider=_make_fake_provider)
        wire_cron_callback(rt)

        rt.agent.process_direct = AsyncMock(return_value="test response")

        from miqi.cron.types import CronJob, CronPayload, CronSchedule

        job = CronJob(
            id="test-job",
            name="Test",
            schedule=CronSchedule(kind="every", every_ms=60000),
            payload=CronPayload(message="hello", deliver=False),
        )

        result = await rt.cron.on_job(job)
        assert result == "test response"
        rt.agent.process_direct.assert_awaited_once_with(
            "hello",
            session_key="cron:test-job",
            channel="cli",
            chat_id="direct",
        )

    @pytest.mark.asyncio
    async def test_cron_callback_publishes_outbound_when_deliver(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime, wire_cron_callback

        rt = create_runtime(config, make_provider=_make_fake_provider)
        wire_cron_callback(rt)

        rt.agent.process_direct = AsyncMock(return_value="delivered response")
        rt.bus.publish_outbound = AsyncMock()

        from miqi.cron.types import CronJob, CronPayload, CronSchedule

        job = CronJob(
            id="deliver-job",
            name="Deliver",
            schedule=CronSchedule(kind="every", every_ms=60000),
            payload=CronPayload(
                message="hello",
                deliver=True,
                channel="feishu",
                to="user123",
            ),
        )

        result = await rt.cron.on_job(job)
        assert result == "delivered response"
        rt.bus.publish_outbound.assert_awaited_once()
        call_args = rt.bus.publish_outbound.call_args[0][0]
        assert call_args.channel == "feishu"
        assert call_args.chat_id == "user123"
        assert call_args.content == "delivered response"

    @pytest.mark.asyncio
    async def test_cron_callback_no_outbound_without_deliver(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MIQI_AGENTS__DEFAULTS__WORKSPACE", str(tmp_path))
        config = Config()

        from miqi.runtime.factory import create_runtime, wire_cron_callback

        rt = create_runtime(config, make_provider=_make_fake_provider)
        wire_cron_callback(rt)

        rt.agent.process_direct = AsyncMock(return_value="no deliver")
        rt.bus.publish_outbound = AsyncMock()

        from miqi.cron.types import CronJob, CronPayload, CronSchedule

        job = CronJob(
            id="no-deliver-job",
            name="NoDeliver",
            schedule=CronSchedule(kind="every", every_ms=60000),
            payload=CronPayload(message="hello", deliver=False),
        )

        await rt.cron.on_job(job)
        rt.bus.publish_outbound.assert_not_awaited()
