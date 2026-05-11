"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair
from openai import AsyncOpenAI

from miqi.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class CustomProvider(LLMProvider):

    supports_streaming: bool = True

    def __init__(self, api_key: str = "no-key", api_base: str = "http://localhost:8000/v1", default_model: str = "default"):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   *, on_delta=None) -> LLMResponse:
        if on_delta is not None and not tools:
            return await self._chat_stream(messages, tools, model, max_tokens, temperature, on_delta=on_delta)
        return await self._chat_block(messages, tools, model, max_tokens, temperature)

    async def _chat_block(self, messages, tools, model, max_tokens, temperature) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    async def _chat_stream(self, messages, tools, model, max_tokens, temperature, *, on_delta) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": True,
        }
        content_parts: list[str] = []
        finish_reason = "stop"
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = choice.delta
                if delta.tool_calls:
                    # Fallback to blocking call on tool_call detection.
                    # No deltas have been flushed yet, so no leak.
                    return await self._chat_block(messages, tools, model, max_tokens, temperature)
                if delta.content:
                    content_parts.append(delta.content)
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except Exception:
            # Stream creation or iteration failed before any delta was
            # flushed.  Fall back to a blocking call.
            return await self._chat_block(messages, tools, model, max_tokens, temperature)

        # Flush buffered deltas to on_delta (safe — no tool calls detected)
        for part in content_parts:
            try:
                await on_delta(part)
            except Exception:
                pass

        return LLMResponse(
            content="".join(content_parts) or None,
            finish_reason=finish_reason,
            streamed=True,
        )

    def _parse(self, response: Any) -> LLMResponse:
        if not response.choices:
            return LLMResponse(content=None, finish_reason="stop")
        choice = response.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in (msg.tool_calls or []):
            args = json_repair.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=args))
        u = response.usage
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model

