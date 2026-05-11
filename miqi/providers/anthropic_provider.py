"""Anthropic provider — uses the official anthropic SDK directly.

Handles the OpenAI→Anthropic message format conversion internally so the rest of
the codebase can always speak OpenAI-format messages.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import anthropic
import json_repair
from loguru import logger

from miqi.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from miqi.providers.registry import find_by_model, find_by_name


class AnthropicProvider(LLMProvider):
    """
    Provider for Anthropic models (claude-*) using the anthropic SDK.

    Accepts the same OpenAI-format messages as the rest of the codebase and
    converts them to Anthropic format internally.
    """

    supports_streaming: bool = True

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        self._selected_spec = find_by_name(provider_name) if provider_name else None
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        client_kwargs: dict[str, Any] = {
            "api_key": api_key or None,
            "default_headers": self.extra_headers,
        }
        if api_base:
            client_kwargs["base_url"] = api_base

        self._client = anthropic.AsyncAnthropic(**client_kwargs)

    # ------------------------------------------------------------------
    # Model name
    # ------------------------------------------------------------------

    def _resolve_model(self, model: str) -> str:
        """Strip 'anthropic/' prefix if present; Anthropic SDK wants bare model names."""
        for prefix in ("anthropic/", "anthropic-"):
            if model.startswith(prefix):
                return model[len(prefix):]
        return model

    # ------------------------------------------------------------------
    # Message format conversion: OpenAI → Anthropic
    # ------------------------------------------------------------------

    def _extract_system_and_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        use_cache_control: bool = False,
    ) -> tuple[list[dict[str, Any]] | str, list[dict[str, Any]]]:
        """Split out system messages and convert the rest to Anthropic format.

        Returns (system, anthropic_messages) where system is either a plain string
        or a list of content blocks (when use_cache_control=True).
        """
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")

            if role == "system":
                content = msg.get("content") or ""
                if isinstance(content, list):
                    # Already may be a list of blocks — extract text
                    system_parts.append(
                        " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict)
                        )
                    )
                else:
                    system_parts.append(str(content))
                continue

            if role == "assistant":
                anthropic_messages.append(self._convert_assistant_msg(msg))
                continue

            if role == "tool":
                anthropic_messages.append(self._convert_tool_result_msg(msg))
                continue

            if role == "user":
                anthropic_messages.append(self._convert_user_msg(msg))
                continue

        # Merge consecutive same-role messages (Anthropic requires alternating roles)
        anthropic_messages = self._merge_consecutive_same_role(anthropic_messages)

        system_text = "\n\n".join(p for p in system_parts if p)

        if not use_cache_control:
            return system_text, anthropic_messages

        # Prompt caching: wrap system text as a content block with cache_control
        system_blocks: list[dict[str, Any]] = []
        if system_text:
            system_blocks = [
                {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
            ]
        return system_blocks, anthropic_messages

    def _convert_user_msg(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Convert an OpenAI user message to Anthropic format."""
        content = msg.get("content")
        if content is None:
            content = "(empty)"
        if isinstance(content, list):
            # May contain text/image blocks — pass through as-is (already compatible)
            return {"role": "user", "content": content}
        return {"role": "user", "content": str(content)}

    def _convert_assistant_msg(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Convert an OpenAI assistant message (possibly with tool_calls) to Anthropic."""
        content_blocks: list[dict[str, Any]] = []

        text = msg.get("content")
        if text:
            content_blocks.append({"type": "text", "text": str(text)})

        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    input_data = json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    input_data = json_repair.loads(raw_args)
            else:
                input_data = raw_args

            if not isinstance(input_data, dict):
                input_data = {}

            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", "tc_unknown"),
                "name": fn.get("name", "unknown"),
                "input": input_data,
            })

        if not content_blocks:
            content_blocks = [{"type": "text", "text": ""}]

        return {"role": "assistant", "content": content_blocks}

    def _convert_tool_result_msg(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Convert an OpenAI tool result message to Anthropic user format."""
        tool_use_id = msg.get("tool_call_id", "tc_unknown")
        content = msg.get("content") or ""

        result_block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": str(content) if not isinstance(content, list) else content,
        }
        return {"role": "user", "content": [result_block]}

    def _merge_consecutive_same_role(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Merge consecutive messages with the same role into one.

        Anthropic requires messages to alternate between 'user' and 'assistant'.
        This happens when tool results and a following user message both become
        role='user' after conversion.
        """
        if not messages:
            return messages

        merged: list[dict[str, Any]] = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                prev = merged[-1]
                # Normalise both contents to lists of blocks
                prev_content = prev["content"]
                new_content = msg["content"]

                if isinstance(prev_content, str):
                    prev_content = [{"type": "text", "text": prev_content}]
                if isinstance(new_content, str):
                    new_content = [{"type": "text", "text": new_content}]

                merged[-1] = {
                    "role": prev["role"],
                    "content": prev_content + new_content,
                }
            else:
                merged.append(msg)

        return merged

    def _convert_tools(
        self,
        tools: list[dict[str, Any]],
        *,
        use_cache_control: bool = False,
    ) -> list[dict[str, Any]]:
        """Convert OpenAI tool definitions to Anthropic format."""
        converted = []
        for tool in tools:
            fn = tool.get("function", tool)
            converted.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

        if use_cache_control and converted:
            converted[-1] = {**converted[-1], "cache_control": {"type": "ephemeral"}}

        return converted

    # ------------------------------------------------------------------
    # Network error detection
    # ------------------------------------------------------------------

    def _is_transient_network_error(self, error: Exception) -> bool:
        msg = str(error).lower()
        signals = (
            "connection reset",
            "connection aborted",
            "temporary failure",
            "timed out",
            "timeout",
            "502",
            "503",
            "504",
            "bad gateway",
            "service unavailable",
            "overloaded",
        )
        return any(s in msg for s in signals)

    # ------------------------------------------------------------------
    # Main interface
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        *,
        on_delta=None,
    ) -> LLMResponse:
        original_model = model or self.default_model
        resolved = self._resolve_model(original_model)
        max_tokens = max(1, max_tokens)

        spec = self._selected_spec or find_by_model(original_model)
        use_cache = bool(spec and spec.supports_prompt_caching)

        clean_messages = self._sanitize_empty_content(messages)
        system, anthropic_messages = self._extract_system_and_messages(
            clean_messages, use_cache_control=use_cache
        )

        kwargs: dict[str, Any] = {
            "model": resolved,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system:
            kwargs["system"] = system

        if tools:
            kwargs["tools"] = self._convert_tools(tools, use_cache_control=use_cache)
            kwargs["tool_choice"] = {"type": "auto"}

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if on_delta is not None:
                    return await self._stream_chat(kwargs, on_delta)
                response = await self._client.messages.create(**kwargs)
                return self._parse_response(response)
            except Exception as e:
                if attempt < max_attempts and self._is_transient_network_error(e):
                    delay = min(30, (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5))
                    await asyncio.sleep(delay)
                    continue
                return LLMResponse(
                    content=f"Error calling LLM: {e}",
                    finish_reason="error",
                )

        return LLMResponse(content="Error calling LLM: retries exhausted", finish_reason="error")

    async def _stream_chat(
        self,
        kwargs: dict[str, Any],
        on_delta,
    ) -> LLMResponse:
        """Stream an Anthropic message, buffering deltas and flushing only when
        the final response has no tool calls.

        Deltas are buffered internally.  If the response ends up containing
        tool calls, all buffered deltas are silently discarded so no partial
        text leaks to the UI.  If the response is pure text, deltas are flushed
        to ``on_delta``.
        """
        content_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        tool_acc: dict[int, dict[str, Any]] = {}  # block index → {id, name, input_json}
        stop_reason = "stop"
        usage: dict[str, int] = {}

        try:
            stream_ctx = self._client.messages.stream(**kwargs)
            stream = await stream_ctx.__aenter__()
        except Exception:
            # Stream creation failed before any delta was emitted.
            # Fall back to a blocking call.
            try:
                response = await self._client.messages.create(**kwargs)
                return self._parse_response(response)
            except Exception as fallback_err:
                return LLMResponse(
                    content=f"Error calling LLM: {fallback_err}",
                    finish_reason="error",
                )

        try:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "text") and delta.text:
                        content_parts.append(delta.text)
                    elif hasattr(delta, "partial_json") and delta.partial_json:
                        idx = event.index
                        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "input_json": ""})
                        acc["input_json"] += delta.partial_json

                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        idx = event.index
                        tool_acc.setdefault(idx, {
                            "id": block.id,
                            "name": block.name,
                            "input_json": "",
                        })

                elif event.type == "message_delta":
                    if event.delta and event.delta.stop_reason:
                        stop_reason = event.delta.stop_reason
                    if event.usage:
                        usage = {
                            "prompt_tokens": event.usage.input_tokens or 0,
                            "completion_tokens": event.usage.output_tokens or 0,
                            "total_tokens": (event.usage.input_tokens or 0) + (event.usage.output_tokens or 0),
                        }
        finally:
            try:
                await stream_ctx.__aexit__(None, None, None)
            except Exception:
                pass

        # Build tool calls from accumulated data
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            raw_input = acc.get("input_json", "")
            try:
                input_data = json.loads(raw_input) if raw_input else {}
            except (json.JSONDecodeError, ValueError):
                input_data = json_repair.loads(raw_input) if raw_input else {}
            if not isinstance(input_data, dict):
                input_data = {}
            tool_calls.append(ToolCallRequest(
                id=acc.get("id", f"tc_stream_{idx}"),
                name=acc.get("name", "unknown"),
                arguments=input_data,
            ))

        content = "".join(content_parts) or None
        stop_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }
        finish_reason = stop_map.get(stop_reason, "stop")

        has_tool_calls = bool(tool_calls)

        # Only flush buffered deltas to on_delta if the response is pure
        # text — no tool calls.
        if not has_tool_calls:
            for part in content_parts:
                try:
                    await on_delta(part)
                except Exception:
                    pass

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            streamed=not has_tool_calls,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Convert an Anthropic Messages response to LLMResponse."""
        tool_calls: list[ToolCallRequest] = []
        text_parts: list[str] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                input_data = block.input
                if isinstance(input_data, str):
                    try:
                        input_data = json.loads(input_data)
                    except (json.JSONDecodeError, ValueError):
                        input_data = json_repair.loads(input_data)

                if not isinstance(input_data, dict):
                    input_data = {}

                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=input_data,
                ))

        content = "\n".join(text_parts) if text_parts else None

        # Map Anthropic stop reasons to OpenAI-style finish_reason
        stop_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }
        finish_reason = stop_map.get(response.stop_reason or "", "stop")

        usage: dict[str, int] = {}
        if hasattr(response, "usage") and response.usage:
            input_tokens = getattr(response.usage, "input_tokens", 0) or 0
            output_tokens = getattr(response.usage, "output_tokens", 0) or 0
            usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def get_default_model(self) -> str:
        return self.default_model
