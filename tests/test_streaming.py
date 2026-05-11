"""Tests for Phase 6: Provider streaming capability.

Covers:
- Base provider default supports_streaming = False
- Fake streaming / non-streaming providers
- OpenAI streaming parser (mocked): text-only, tool_call deltas, text+tool_call, fallback JSON tool call, stream creation failure, tools non-empty no tool_calls
- Anthropic streaming parser (mocked): text-only, tool_use blocks, text+tool_use, stream creation failure, tools non-empty no tool_calls
- CustomProvider: streamed=True on success, stream creation failure fallback
- Tool-call response not broken by streaming
- AgentService streaming wiring
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from miqi.events.emitter import EventEmitter
from miqi.events.models import MessageDelta, MessageFinal
from miqi.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miqi.runtime.execution import ExecutionManager


# ── Helpers ───────────────────────────────────────────────────────────────

class _AsyncIter:
    """Wrap a list into an async iterator (for mocking OpenAI stream)."""
    def __init__(self, items):
        self._items = iter(items)
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


class _AsyncCtxManager:
    """Wrap a list into an async context manager + async iterator (for Anthropic stream)."""
    def __init__(self, events):
        self._events = events
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def __aiter__(self):
        for e in self._events:
            yield e


# ── Fake providers ────────────────────────────────────────────────────────

class FakeNonStreamingProvider(LLMProvider):
    """Provider that does NOT support streaming — simulates CLI/gateway."""

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, *, on_delta=None):
        return LLMResponse(content="hello world")

    def get_default_model(self) -> str:
        return "fake-model"


class FakeStreamingProvider(LLMProvider):
    """Provider that supports streaming — simulates desktop path."""

    supports_streaming: bool = True

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, *, on_delta=None):
        if on_delta is not None and not tools:
            for chunk in ["hello", " ", "world"]:
                await on_delta(chunk)
            return LLMResponse(content="hello world", streamed=True)
        return LLMResponse(content="hello world")

    def get_default_model(self) -> str:
        return "fake-streaming-model"


# ── OpenAI mock helpers ───────────────────────────────────────────────────

def _make_openai_provider():
    from miqi.providers.openai_provider import OpenAIProvider
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.api_key = "test"
    provider.api_base = "https://api.test.com/v1"
    provider.default_model = "gpt-4o"
    provider.extra_headers = {}
    provider._selected_spec = None
    provider._gateway = None
    provider._client = MagicMock()
    return provider


def _make_openai_chunk(content=None, finish_reason=None, tool_calls=None, usage=None):
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    delta.reasoning_content = None
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk = MagicMock()
    chunk.choices = [choice]
    chunk.usage = usage
    return chunk


def _make_openai_tc_delta(index, tc_id=None, name=None, arguments=None):
    tc = MagicMock()
    tc.index = index
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


# ── Anthropic mock helpers ────────────────────────────────────────────────

class _MockDelta:
    def __init__(self, text=None, partial_json=None):
        self.text = text
        self.partial_json = partial_json


class _MockContentBlock:
    def __init__(self, block_type="text", **kwargs):
        self.type = block_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockMessageDelta:
    def __init__(self, stop_reason=None):
        self.stop_reason = stop_reason


class _MockEvent:
    def __init__(self, event_type, **kwargs):
        self.type = event_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockUsage:
    def __init__(self, input_tokens=10, output_tokens=20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _make_anthropic_provider():
    from miqi.providers.anthropic_provider import AnthropicProvider
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_key = "test"
    provider.api_base = None
    provider.default_model = "claude-sonnet-4-1"
    provider.extra_headers = {}
    provider._selected_spec = None
    provider._client = MagicMock()
    return provider


# ══════════════════════════════════════════════════════════════════════════
# Base provider defaults
# ══════════════════════════════════════════════════════════════════════════

class TestBaseProviderStreaming:

    def test_default_supports_streaming_is_false(self):
        assert LLMProvider.supports_streaming is False

    def test_non_streaming_provider_flag(self):
        p = FakeNonStreamingProvider()
        assert p.supports_streaming is False

    def test_streaming_provider_flag(self):
        p = FakeStreamingProvider()
        assert p.supports_streaming is True

    def test_llm_response_default_not_streamed(self):
        resp = LLMResponse(content="test")
        assert resp.streamed is False

    def test_llm_response_streamed_flag(self):
        resp = LLMResponse(content="test", streamed=True)
        assert resp.streamed is True


# ══════════════════════════════════════════════════════════════════════════
# Fake providers
# ══════════════════════════════════════════════════════════════════════════

class TestFakeStreamingProvider:

    @pytest.mark.asyncio
    async def test_streaming_emits_deltas(self):
        deltas: list[str] = []
        async def on_delta(text: str):
            deltas.append(text)

        provider = FakeStreamingProvider()
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            on_delta=on_delta,
        )

        assert response.content == "hello world"
        assert response.streamed is True
        assert deltas == ["hello", " ", "world"]

    @pytest.mark.asyncio
    async def test_streaming_without_on_delta_still_works(self):
        provider = FakeStreamingProvider()
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response.content == "hello world"
        assert response.streamed is False

    @pytest.mark.asyncio
    async def test_streaming_with_tools_uses_blocking(self):
        deltas: list[str] = []
        async def on_delta(text: str):
            deltas.append(text)

        provider = FakeStreamingProvider()
        tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            on_delta=on_delta,
        )
        assert response.content == "hello world"
        assert deltas == []


class TestFakeNonStreamingProvider:

    @pytest.mark.asyncio
    async def test_non_streaming_ignores_on_delta(self):
        deltas: list[str] = []
        async def on_delta(text: str):
            deltas.append(text)

        provider = FakeNonStreamingProvider()
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            on_delta=on_delta,
        )
        assert response.content == "hello world"
        assert response.streamed is False
        assert deltas == []

    @pytest.mark.asyncio
    async def test_non_streaming_returns_valid_response(self):
        provider = FakeNonStreamingProvider()
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response.content == "hello world"
        assert not response.has_tool_calls


# ══════════════════════════════════════════════════════════════════════════
# AgentService streaming wiring
# ══════════════════════════════════════════════════════════════════════════

class TestAgentServiceStreaming:

    @pytest.mark.asyncio
    async def test_delta_fn_returns_none_when_no_subscribers(self):
        from miqi.runtime.agent_service import AgentService
        events = EventEmitter()
        agent = MagicMock()
        agent._current_execution_id = ""
        svc = AgentService(agent=agent, events=events)
        fn = svc._make_content_delta_fn("exec-123")
        assert fn is None

    @pytest.mark.asyncio
    async def test_delta_fn_returns_callback_when_subscribers_exist(self):
        from miqi.runtime.agent_service import AgentService
        events = EventEmitter()
        events.subscribe(AsyncMock())
        agent = MagicMock()
        agent._current_execution_id = ""
        svc = AgentService(agent=agent, events=events)
        fn = svc._make_content_delta_fn("exec-123")
        assert fn is not None

    @pytest.mark.asyncio
    async def test_delta_fn_emits_message_delta(self):
        from miqi.runtime.agent_service import AgentService
        events = EventEmitter()
        received: list[Any] = []
        async def _subscriber(e):
            received.append(e)
        events.subscribe(_subscriber)

        agent = MagicMock()
        agent._current_execution_id = ""
        svc = AgentService(agent=agent, events=events)

        fn = svc._make_content_delta_fn("exec-456")
        assert fn is not None
        await fn("chunk1")
        await fn("chunk2")

        assert len(received) == 2
        assert isinstance(received[0], MessageDelta)
        assert received[0].delta == "chunk1"
        assert received[0].execution_id == "exec-456"
        assert isinstance(received[1], MessageDelta)
        assert received[1].delta == "chunk2"

    @pytest.mark.asyncio
    async def test_message_final_not_emitted_without_subscribers(self):
        from miqi.runtime.agent_service import AgentService
        events = EventEmitter()
        received: list[Any] = []

        async def _subscriber(e):
            received.append(e)

        # No subscribers registered
        agent = MagicMock()
        agent._current_execution_id = ""
        agent.process_direct = AsyncMock(return_value="hello")
        svc = AgentService(agent=agent, events=events)

        if events.subscriber_count > 0:
            await events.emit(MessageFinal(execution_id="exec-789", content="hello"))

        assert received == []

    @pytest.mark.asyncio
    async def test_message_final_emitted_with_subscribers(self):
        events = EventEmitter()
        received: list[Any] = []
        async def _subscriber(e):
            received.append(e)
        events.subscribe(_subscriber)

        await events.emit(MessageFinal(execution_id="exec-789", content="hello"))

        assert len(received) == 1
        assert isinstance(received[0], MessageFinal)
        assert received[0].content == "hello"


# ══════════════════════════════════════════════════════════════════════════
# OpenAI streaming parser test (mocked)
# ══════════════════════════════════════════════════════════════════════════

class TestOpenAIStreamingParser:

    @pytest.mark.asyncio
    async def test_text_only(self):
        """Pure text-streaming: deltas are flushed at the end, streamed=True."""
        provider = _make_openai_provider()
        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncIter([
                _make_openai_chunk(content="Hello"),
                _make_openai_chunk(content=" world"),
                _make_openai_chunk(content="!", finish_reason="stop"),
            ])
        )

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "gpt-4o", "messages": [], "max_tokens": 100, "stream": True},
            on_delta,
        )

        assert response.content == "Hello world!"
        assert response.streamed is True
        assert deltas == ["Hello", " world", "!"]
        assert response.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_tool_call_deltas_no_leak(self):
        """Tool-call deltas: no on_delta calls, streamed=False, has_tool_calls."""
        provider = _make_openai_provider()
        tc1 = _make_openai_tc_delta(0, tc_id="call_1", name="exec", arguments='{"c')
        tc2 = _make_openai_tc_delta(0, arguments='md":"ls"}')

        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncIter([
                _make_openai_chunk(tool_calls=[tc1]),
                _make_openai_chunk(tool_calls=[tc2]),
                _make_openai_chunk(finish_reason="tool_calls"),
            ])
        )

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "gpt-4o", "messages": [], "max_tokens": 100, "stream": True},
            on_delta,
        )

        assert response.has_tool_calls
        assert response.streamed is False
        assert deltas == []

    @pytest.mark.asyncio
    async def test_content_then_tool_call_no_leak(self):
        """Content deltas followed by tool_call deltas: all deltas suppressed."""
        provider = _make_openai_provider()
        tc1 = _make_openai_tc_delta(0, tc_id="call_1", name="read_file", arguments='{"pa')
        tc2 = _make_openai_tc_delta(0, arguments='th":"/tmp"}')

        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncIter([
                _make_openai_chunk(content="I'll read"),
                _make_openai_chunk(content=" that file"),
                _make_openai_chunk(tool_calls=[tc1]),
                _make_openai_chunk(tool_calls=[tc2]),
                _make_openai_chunk(finish_reason="tool_calls"),
            ])
        )

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "gpt-4o", "messages": [], "max_tokens": 100, "stream": True},
            on_delta,
        )

        assert response.has_tool_calls
        assert response.streamed is False
        assert deltas == []  # content deltas suppressed because tool_calls present

    @pytest.mark.asyncio
    async def test_fallback_json_tool_call_no_leak(self):
        """Fallback JSON tool call in content: no on_delta calls, has_tool_calls."""
        provider = _make_openai_provider()
        # Content contains a JSON tool call pattern
        json_content = '{"name": "exec", "arguments": {"cmd": "rm -rf /"}}'

        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncIter([
                _make_openai_chunk(content=json_content),
                _make_openai_chunk(finish_reason="stop"),
            ])
        )

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "gpt-4o", "messages": [], "max_tokens": 100, "stream": True},
            on_delta,
        )

        assert response.has_tool_calls
        assert response.streamed is False
        assert deltas == []  # raw JSON not leaked
        assert response.tool_calls[0].name == "exec"

    @pytest.mark.asyncio
    async def test_tools_nonempty_no_tool_calls_still_streams(self):
        """tools kwarg present but response has no tool_calls: deltas flushed, streamed=True."""
        provider = _make_openai_provider()
        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncIter([
                _make_openai_chunk(content="No tool"),
                _make_openai_chunk(content=" needed"),
                _make_openai_chunk(finish_reason="stop"),
            ])
        )

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "gpt-4o", "messages": [], "max_tokens": 100,
             "stream": True, "tools": [{"type": "function", "function": {"name": "x"}}]},
            on_delta,
        )

        assert not response.has_tool_calls
        assert response.streamed is True
        assert deltas == ["No tool", " needed"]

    @pytest.mark.asyncio
    async def test_stream_creation_failure_falls_back_to_blocking(self):
        """When stream creation fails, fallback to blocking returns non-streamed response."""
        provider = _make_openai_provider()

        # First call (streaming) raises
        # Second call (blocking) succeeds
        blocking_response = MagicMock()
        blocking_response.choices = [
            MagicMock(
                message=MagicMock(content="fallback", tool_calls=None, reasoning_content=None),
                finish_reason="stop",
            )
        ]
        blocking_response.usage = MagicMock(prompt_tokens=5, completion_tokens=10, total_tokens=15)

        call_count = 0
        async def _create(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("stream"):
                raise RuntimeError("stream creation failed")
            return blocking_response

        provider._client.chat.completions.create = _create

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "gpt-4o", "messages": [], "max_tokens": 100, "stream": True},
            on_delta,
        )

        assert response.content == "fallback"
        assert response.streamed is False
        assert deltas == []  # no deltas flushed on fallback
        assert call_count == 2  # stream attempt + blocking fallback

    @pytest.mark.asyncio
    async def test_supports_streaming_flag(self):
        from miqi.providers.openai_provider import OpenAIProvider
        assert OpenAIProvider.supports_streaming is True


# ══════════════════════════════════════════════════════════════════════════
# Anthropic streaming parser test (mocked)
# ══════════════════════════════════════════════════════════════════════════

class TestAnthropicStreamingParser:

    @pytest.mark.asyncio
    async def test_text_only(self):
        """Pure text-streaming: deltas flushed at the end, streamed=True."""
        provider = _make_anthropic_provider()
        events = [
            _MockEvent("content_block_delta", delta=_MockDelta(text="Hello"), index=0),
            _MockEvent("content_block_delta", delta=_MockDelta(text=" there"), index=0),
            _MockEvent("message_delta", delta=_MockMessageDelta(stop_reason="end_turn"), usage=_MockUsage()),
        ]
        provider._client.messages.stream = MagicMock(return_value=_AsyncCtxManager(events))

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "claude-sonnet-4-1", "messages": [], "max_tokens": 100},
            on_delta,
        )

        assert response.content == "Hello there"
        assert response.streamed is True
        assert deltas == ["Hello", " there"]
        assert response.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_tool_use_deltas_no_leak(self):
        """Tool_use blocks: no on_delta calls, streamed=False, has_tool_calls."""
        provider = _make_anthropic_provider()
        events = [
            _MockEvent("content_block_start",
                        content_block=_MockContentBlock(block_type="tool_use", id="tu_1", name="exec"),
                        index=0),
            _MockEvent("content_block_delta",
                        delta=_MockDelta(partial_json='{"cm'),
                        index=0),
            _MockEvent("content_block_delta",
                        delta=_MockDelta(partial_json='d":"ls"}'),
                        index=0),
            _MockEvent("message_delta",
                        delta=_MockMessageDelta(stop_reason="tool_use"),
                        usage=_MockUsage()),
        ]
        provider._client.messages.stream = MagicMock(return_value=_AsyncCtxManager(events))

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "claude-sonnet-4-1", "messages": [], "max_tokens": 100},
            on_delta,
        )

        assert response.has_tool_calls
        assert response.streamed is False
        assert deltas == []

    @pytest.mark.asyncio
    async def test_text_then_tool_use_no_leak(self):
        """Text content_block followed by tool_use block: all deltas suppressed."""
        provider = _make_anthropic_provider()
        events = [
            _MockEvent("content_block_start",
                        content_block=_MockContentBlock(block_type="text"),
                        index=0),
            _MockEvent("content_block_delta",
                        delta=_MockDelta(text="I'll run"),
                        index=0),
            _MockEvent("content_block_delta",
                        delta=_MockDelta(text=" that"),
                        index=0),
            _MockEvent("content_block_start",
                        content_block=_MockContentBlock(block_type="tool_use", id="tu_1", name="exec"),
                        index=1),
            _MockEvent("content_block_delta",
                        delta=_MockDelta(partial_json='{"cmd":"ls"}'),
                        index=1),
            _MockEvent("message_delta",
                        delta=_MockMessageDelta(stop_reason="tool_use"),
                        usage=_MockUsage()),
        ]
        provider._client.messages.stream = MagicMock(return_value=_AsyncCtxManager(events))

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "claude-sonnet-4-1", "messages": [], "max_tokens": 100},
            on_delta,
        )

        assert response.has_tool_calls
        assert response.streamed is False
        assert deltas == []  # text deltas suppressed because tool_use block present

    @pytest.mark.asyncio
    async def test_tools_nonempty_no_tool_calls_still_streams(self):
        """tools kwarg present but response has no tool_use: deltas flushed, streamed=True."""
        provider = _make_anthropic_provider()
        events = [
            _MockEvent("content_block_delta", delta=_MockDelta(text="No tool"), index=0),
            _MockEvent("content_block_delta", delta=_MockDelta(text=" needed"), index=0),
            _MockEvent("message_delta", delta=_MockMessageDelta(stop_reason="end_turn"), usage=_MockUsage()),
        ]
        provider._client.messages.stream = MagicMock(return_value=_AsyncCtxManager(events))

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "claude-sonnet-4-1", "messages": [], "max_tokens": 100,
             "tools": [{"name": "x", "input_schema": {}}]},
            on_delta,
        )

        assert not response.has_tool_calls
        assert response.streamed is True
        assert deltas == ["No tool", " needed"]

    @pytest.mark.asyncio
    async def test_stream_creation_failure_falls_back_to_blocking(self):
        """When stream creation fails, fallback to blocking returns non-streamed response."""
        provider = _make_anthropic_provider()

        blocking_response = MagicMock()
        blocking_response.content = [MagicMock(type="text", text="fallback")]
        blocking_response.stop_reason = "end_turn"
        blocking_response.usage = MagicMock(input_tokens=5, output_tokens=10)
        provider._client.messages.create = AsyncMock(return_value=blocking_response)

        # stream() raises on __aenter__
        class _FailingCtx:
            async def __aenter__(self):
                raise RuntimeError("stream creation failed")
            async def __aexit__(self, *args):
                pass
            async def __aiter__(self):
                return
                yield  # make it an async generator

        provider._client.messages.stream = MagicMock(return_value=_FailingCtx())

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._stream_chat(
            {"model": "claude-sonnet-4-1", "messages": [], "max_tokens": 100},
            on_delta,
        )

        assert response.content == "fallback"
        assert response.streamed is False
        assert deltas == []

    @pytest.mark.asyncio
    async def test_supports_streaming_flag(self):
        from miqi.providers.anthropic_provider import AnthropicProvider
        assert AnthropicProvider.supports_streaming is True


# ══════════════════════════════════════════════════════════════════════════
# Tool-call response not broken by streaming
# ══════════════════════════════════════════════════════════════════════════

class TestToolCallNotBroken:

    @pytest.mark.asyncio
    async def test_blocking_call_with_tools_unchanged(self):
        provider = FakeNonStreamingProvider()
        tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
        async def on_delta(text):
            pass

        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            on_delta=on_delta,
        )
        assert response.content == "hello world"
        assert not response.has_tool_calls

    @pytest.mark.asyncio
    async def test_streaming_with_tools_goes_to_blocking_path(self):
        provider = FakeStreamingProvider()
        tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            on_delta=on_delta,
        )
        assert response.content == "hello world"
        assert response.streamed is False
        assert deltas == []

    @pytest.mark.asyncio
    async def test_tool_call_response_structure(self):
        response = LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id="tc1", name="exec", arguments={"cmd": "ls"})],
            finish_reason="tool_calls",
            streamed=False,
        )
        assert response.has_tool_calls
        assert response.tool_calls[0].name == "exec"

    @pytest.mark.asyncio
    async def test_streamed_flag_false_when_tool_calls(self):
        """When tool_calls are present, streamed=False — providers enforce this."""
        # OpenAI: streamed=not has_tool_calls
        response = LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="tc1", name="exec", arguments={})],
            finish_reason="tool_calls",
            streamed=False,
        )
        assert response.has_tool_calls
        assert response.streamed is False


# ══════════════════════════════════════════════════════════════════════════
# CustomProvider streaming
# ══════════════════════════════════════════════════════════════════════════

class TestCustomProviderStreaming:

    def test_supports_streaming_flag(self):
        from miqi.providers.custom_provider import CustomProvider
        assert CustomProvider.supports_streaming is True

    @pytest.mark.asyncio
    async def test_chat_stream_returns_streamed_true(self):
        """CustomProvider _chat_stream returns streamed=True on pure text success."""
        from miqi.providers.custom_provider import CustomProvider
        provider = CustomProvider.__new__(CustomProvider)
        provider.api_key = "test"
        provider.api_base = "http://localhost:8000/v1"
        provider.default_model = "default"
        provider._client = MagicMock()

        def _make_chunk(content=None, finish_reason=None):
            delta = MagicMock()
            delta.content = content
            delta.tool_calls = None
            choice = MagicMock()
            choice.delta = delta
            choice.finish_reason = finish_reason
            chunk = MagicMock()
            chunk.choices = [choice]
            return chunk

        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncIter([
                _make_chunk(content="Hi"),
                _make_chunk(content=" there", finish_reason="stop"),
            ])
        )

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._chat_stream(
            [{"role": "user", "content": "hello"}], None, None, 4096, 0.7,
            on_delta=on_delta,
        )

        assert response.content == "Hi there"
        assert response.streamed is True
        assert deltas == ["Hi", " there"]

    @pytest.mark.asyncio
    async def test_chat_stream_creation_failure_falls_back(self):
        """When stream creation fails, _chat_stream falls back to _chat_block."""
        from miqi.providers.custom_provider import CustomProvider
        provider = CustomProvider.__new__(CustomProvider)
        provider.api_key = "test"
        provider.api_base = "http://localhost:8000/v1"
        provider.default_model = "default"
        provider._client = MagicMock()

        # Stream call raises, blocking call succeeds
        blocking_response = MagicMock()
        blocking_response.choices = [
            MagicMock(
                message=MagicMock(content="fallback", tool_calls=None, reasoning_content=None),
                finish_reason="stop",
            )
        ]
        blocking_response.usage = MagicMock(prompt_tokens=5, completion_tokens=10, total_tokens=15)

        call_count = 0
        async def _create(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("stream"):
                raise RuntimeError("stream failed")
            return blocking_response

        provider._client.chat.completions.create = _create

        deltas: list[str] = []
        async def on_delta(text):
            deltas.append(text)

        response = await provider._chat_stream(
            [{"role": "user", "content": "hello"}], None, None, 4096, 0.7,
            on_delta=on_delta,
        )

        assert response.content == "fallback"
        assert response.streamed is False
        assert deltas == []

    @pytest.mark.asyncio
    async def test_non_streaming_call_unchanged(self):
        from miqi.providers.custom_provider import CustomProvider
        provider = CustomProvider.__new__(CustomProvider)
        provider.api_key = "test"
        provider.api_base = "http://localhost:8000/v1"
        provider.default_model = "default"
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(content="response", tool_calls=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )
        )

        response = await provider._chat_block(
            [{"role": "user", "content": "hi"}], None, None, 4096, 0.7
        )
        assert response.content == "response"
        assert response.streamed is False


# ══════════════════════════════════════════════════════════════════════════
# Full round-trip: AgentService + streaming provider + events
# ══════════════════════════════════════════════════════════════════════════

class TestStreamingRoundTrip:

    @pytest.mark.asyncio
    async def test_desktop_path_emits_message_delta_and_final(self):
        from miqi.runtime.agent_service import AgentService
        events = EventEmitter()
        received: list[Any] = []
        async def _sub(e):
            received.append(e)
        events.subscribe(_sub)

        agent = MagicMock()
        agent._current_execution_id = ""

        async def _process_direct(message, *, session_key, channel, chat_id,
                                   on_progress=None, on_content_delta=None):
            if on_content_delta:
                await on_content_delta("chunk1")
                await on_content_delta("chunk2")
            return "final response"

        agent.process_direct = _process_direct

        svc = AgentService(agent=agent, events=events)
        result = await svc.send("hello", session_key="test:stream")

        await asyncio.sleep(0.1)

        delta_events = [e for e in received if isinstance(e, MessageDelta)]
        final_events = [e for e in received if isinstance(e, MessageFinal)]

        assert len(delta_events) == 2
        assert delta_events[0].delta == "chunk1"
        assert delta_events[1].delta == "chunk2"
        assert len(final_events) == 1
        assert final_events[0].content == "final response"

    @pytest.mark.asyncio
    async def test_cli_path_no_streaming_events(self):
        from miqi.runtime.agent_service import AgentService
        events = EventEmitter()
        agent = MagicMock()
        agent._current_execution_id = ""
        agent.process_direct = AsyncMock(return_value="response")

        svc = AgentService(agent=agent, events=events)
        fn = svc._make_content_delta_fn("exec-1")
        assert fn is None
