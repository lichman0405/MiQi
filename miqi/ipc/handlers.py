"""RPC method dispatch — maps method names to handler functions.

Each handler receives the ``Runtime`` object and request params,
and returns a JSON-serializable result dict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from miqi.ipc.protocol import (
    ERROR_EXECUTION_BUSY,
    ERROR_INTERNAL,
    ERROR_METHOD_NOT_FOUND,
    ERROR_INVALID_PARAMS,
    JsonRpcRequest,
    JsonRpcResponse,
    make_error_response,
    make_success_response,
)

if TYPE_CHECKING:
    from miqi.runtime.factory import Runtime

from miqi.runtime.agent_service import AgentBusyError


REDACTED_VALUE = "********"
SENSITIVE_MAPPING_KEYS = {"env", "headers", "extraheaders", "extra_headers"}


def _is_sensitive_field(key: str) -> bool:
    normalized = key.replace("_", "").lower()
    return normalized.endswith(("apikey", "token", "secret", "password"))


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in SENSITIVE_MAPPING_KEYS and isinstance(item, dict):
                redacted[key] = {
                    env_key: REDACTED_VALUE if env_value else env_value
                    for env_key, env_value in item.items()
                }
            elif _is_sensitive_field(str(key)):
                redacted[key] = REDACTED_VALUE if item else item
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


class RpcDispatcher:
    """Route JSON-RPC requests to typed handler methods."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._methods: dict[str, Any] = {
            "app.status": self._app_status,
            "config.read": self._config_read,
            "tool.list": self._tool_list,
            "session.list": self._session_list,
            "session.create": self._session_create,
            "session.rename": self._session_rename,
            "session.archive": self._session_archive,
            "session.unarchive": self._session_unarchive,
            "session.delete": self._session_delete,
            "session.search": self._session_search,
            "session.load": self._session_load,
            "workspace.status": self._workspace_status,
            "workspace.list": self._workspace_list,
            "workspace.open": self._workspace_open,
            "workspace.index": self._workspace_index,
            "workspace.preview": self._workspace_preview,
            "workspace.pinFile": self._workspace_pin_file,
            "workspace.unpinFile": self._workspace_unpin_file,
            "workspace.listPinned": self._workspace_list_pinned,
            "workspace.listRecent": self._workspace_list_recent,
            "memory.status": self._memory_status,
            "memory.search": self._memory_search,
            "memory.update": self._memory_update,
            "memory.remember": self._memory_remember,
            "memory.appendToday": self._memory_append_today,
            "memory.learnLesson": self._memory_learn_lesson,
            "memory.listSnapshot": self._memory_list_snapshot,
            "memory.listLessons": self._memory_list_lessons,
            "memory.deleteSnapshotItem": self._memory_delete_snapshot_item,
            "memory.deleteLesson": self._memory_delete_lesson,
            "memory.setLessonEnabled": self._memory_set_lesson_enabled,
            "context.status": self._context_status,
            "context.listBootstrap": self._context_list_bootstrap,
            "context.readBootstrap": self._context_read_bootstrap,
            "context.listSkills": self._context_list_skills,
            "config.write": self._config_write,
            "config.testProvider": self._config_test_provider,
            "mcp.status": self._mcp_status,
            "cron.list": self._cron_list,
            "cron.add": self._cron_add,
            "cron.update": self._cron_update,
            "cron.delete": self._cron_delete,
            "heartbeat.status": self._heartbeat_status,
            "heartbeat.update": self._heartbeat_update,
            "chat.send": self._chat_send,
            "chat.cancel": self._chat_cancel,
            "chat.regenerate": self._chat_regenerate,
            "chat.approve": self._chat_approve,
            "chat.deny": self._chat_deny,
        }

    @property
    def method_names(self) -> list[str]:
        return sorted(self._methods.keys())

    async def dispatch(self, request: JsonRpcRequest) -> JsonRpcResponse:
        """Dispatch a request and return a response."""
        handler = self._methods.get(request.method)
        if handler is None:
            return make_error_response(
                request.id, ERROR_METHOD_NOT_FOUND,
                f"Method not found: {request.method}",
            )

        try:
            result = await handler(request.params)
            return make_success_response(request.id, result)
        except AgentBusyError:
            return make_error_response(
                request.id, ERROR_EXECUTION_BUSY,
                "Execution busy: another execution is already active",
            )
        except Exception as exc:
            return make_error_response(
                request.id, ERROR_INTERNAL,
                f"Internal error: {exc}",
            )

    async def _emit_memory_changed(self, action: str) -> None:
        events = getattr(self._runtime, "events", None)
        if events is None:
            return
        from miqi.events.models import MemoryChanged

        await events.emit(MemoryChanged(action=action))

    async def _emit_cron_changed(
        self,
        *,
        job_id: str = "",
        job_name: str = "",
        action: str = "updated",
    ) -> None:
        events = getattr(self._runtime, "events", None)
        if events is None:
            return
        from miqi.events.models import CronJobChanged

        await events.emit(CronJobChanged(
            job_id=job_id,
            job_name=job_name,
            action=action,
        ))

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _app_status(self, params: dict[str, Any]) -> dict[str, Any]:
        rt = self._runtime
        config = rt.config
        # Use the live workspace (may differ from config after workspace.open)
        if rt.workspace_service is not None:
            workspace = rt.workspace_service.status()["project_root"]
        else:
            workspace = str(config.workspace_path)
        return {
            "status": "running",
            "model": config.agents.defaults.model,
            "workspace": workspace,
            "agent_name": config.agents.defaults.name,
        }

    async def _config_read(self, params: dict[str, Any]) -> dict[str, Any]:
        config = self._runtime.config
        return _redact_sensitive(config.model_dump(by_alias=True))

    async def _tool_list(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = self._runtime.agent
        definitions = agent.tools.get_definitions() if agent.tools else []
        return {
            "tools": definitions,
            "count": len(definitions),
        }

    async def _config_write(self, params: dict[str, Any]) -> dict[str, Any]:
        """Write partial config updates and persist to disk.

        Updates must be a nested dict matching the config schema.  Nested
        keys may use either snake_case (Python field names) or camelCase
        (JSON alias names).  Dot-path keys are not accepted.  Unknown keys
        are rejected.
        """
        from pydantic import BaseModel
        from miqi.config.loader import save_config

        updates = params.get("updates")
        if not updates or not isinstance(updates, dict):
            raise ValueError("params.updates is required and must be a dict")

        config = self._runtime.config

        def _collect_allowed_keys(model_cls: type[BaseModel]) -> set[str]:
            """Collect all accepted field names and aliases for a model."""
            allowed: set[str] = set()
            for field_name, field_info in model_cls.model_fields.items():
                allowed.add(field_name)
                if field_info.alias:
                    allowed.add(field_info.alias)
            return allowed

        def _validate_keys(source: dict, model_cls: type[BaseModel], path: str = "") -> None:
            """Recursively validate that all keys in source are known fields."""
            allowed = _collect_allowed_keys(model_cls)
            for key, value in source.items():
                if key not in allowed:
                    location = f"{path}.{key}" if path else key
                    raise ValueError(f"Unknown config key: {location}")
                if isinstance(value, dict):
                    # Find the sub-model class for this field
                    for field_name, field_info in model_cls.model_fields.items():
                        if field_name == key or field_info.alias == key:
                            sub_ann = field_info.annotation
                            # Unwrap Optional / annotations
                            if sub_ann and isinstance(sub_ann, type) and issubclass(sub_ann, BaseModel):
                                sub_path = f"{path}.{key}" if path else key
                                _validate_keys(value, sub_ann, sub_path)
                            break

        config_dict = config.model_dump()

        def _deep_merge(target: dict, source: dict) -> dict:
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    _deep_merge(target[key], value)
                else:
                    target[key] = value
            return target

        # Validate keys before merging
        _validate_keys(updates, type(config))

        merged = _deep_merge(config_dict, updates)
        try:
            new_config = type(config).model_validate(merged)
        except Exception as exc:
            raise ValueError(f"Invalid config: {exc}")

        self._runtime.config = new_config
        save_config(new_config)
        return {"success": True}

    async def _config_test_provider(self, params: dict[str, Any]) -> dict[str, Any]:
        """Test a provider config by making a minimal chat request.

        When no explicit api_key/api_base is provided, uses the runtime's
        current provider.  Otherwise constructs a **temporary** provider
        from the given credentials — the runtime config is never modified.
        """
        from miqi.providers.base import LLMResponse
        from miqi.providers.registry import find_by_name

        provider_name = params.get("provider")
        model = params.get("model")
        api_key = params.get("api_key", "")
        api_base = params.get("api_base")

        if not provider_name:
            raise ValueError("params.provider is required")

        try:
            if api_key or api_base:
                # Build a temporary provider from the given credentials.
                # The model string (e.g. "openai/gpt-4o") determines the
                # provider type; the explicit api_key/api_base override
                # whatever is in the runtime config.
                model_str = model or f"{provider_name}/test"
                spec = find_by_name(provider_name)
                if spec is None:
                    return {"success": False, "error": f"Unknown provider '{provider_name}'"}

                if spec.provider_type == "anthropic":
                    from miqi.providers.anthropic_provider import AnthropicProvider
                    provider = AnthropicProvider(
                        api_key=api_key, api_base=api_base,
                        provider_name=provider_name, default_model=model_str,
                    )
                elif spec.provider_type == "gemini":
                    from miqi.providers.gemini_provider import GeminiProvider
                    provider = GeminiProvider(
                        api_key=api_key, api_base=api_base,
                        provider_name=provider_name, default_model=model_str,
                    )
                else:
                    from miqi.providers.openai_provider import OpenAIProvider
                    provider = OpenAIProvider(
                        api_key=api_key, api_base=api_base,
                        provider_name=provider_name, default_model=model_str,
                    )
            else:
                # Use the runtime's current provider (respects fake providers in tests)
                provider = self._runtime.provider

            response: LLMResponse = await provider.chat(
                messages=[{"role": "user", "content": "Hi"}],
                model=model,
                max_tokens=5,
                temperature=0.0,
            )
            return {"success": True, "model": model or "", "preview": (response.content or "")[:100]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _mcp_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Report MCP server connection status."""
        agent = self._runtime.agent
        servers_config = self._runtime.config.tools.mcp_servers
        server_statuses: dict[str, Any] = {}
        for name in servers_config:
            server_statuses[name] = {
                "configured": True,
                "connected": getattr(agent, "_mcp_connected", False),
            }
        connected = getattr(agent, "_mcp_connected", False)
        connecting = getattr(agent, "_mcp_connecting", False)
        retry_after = getattr(agent, "_mcp_retry_after", 0.0)
        return {
            "connected": connected,
            "connecting": connecting,
            "servers": server_statuses,
            "retry_after": retry_after,
        }

    async def _session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            return {"sessions": [], "count": 0}
        return svc.list_sessions(include_archived=params.get("include_archived", False))

    async def _session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            raise RuntimeError("SessionService not available on this runtime")
        key = params.get("key")
        if not key:
            raise ValueError("params.key is required")
        return svc.create_session(key, title=params.get("title"))

    async def _session_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            raise RuntimeError("SessionService not available on this runtime")
        key = params.get("key")
        title = params.get("title")
        if not key:
            raise ValueError("params.key is required")
        if not title:
            raise ValueError("params.title is required")
        return svc.rename_session(key, title)

    async def _session_archive(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            raise RuntimeError("SessionService not available on this runtime")
        key = params.get("key")
        if not key:
            raise ValueError("params.key is required")
        return svc.archive_session(key)

    async def _session_unarchive(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            raise RuntimeError("SessionService not available on this runtime")
        key = params.get("key")
        if not key:
            raise ValueError("params.key is required")
        return svc.unarchive_session(key)

    async def _session_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            raise RuntimeError("SessionService not available on this runtime")
        key = params.get("key")
        if not key:
            raise ValueError("params.key is required")
        return svc.delete_session(key)

    async def _session_search(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            return {"sessions": [], "count": 0, "query": params.get("query", "")}
        query = params.get("query", "")
        return svc.search_sessions(query, include_archived=params.get("include_archived", False))

    async def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.session_service
        if svc is None:
            raise RuntimeError("SessionService not available on this runtime")
        key = params.get("key")
        if not key:
            raise ValueError("params.key is required")
        return svc.load_session(key)

    async def _chat_send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Submit a message via AgentService, returning execution_id."""
        message = params.get("message")
        if not message:
            raise ValueError("params.message is required")

        svc = self._runtime.agent_service
        if svc is None:
            raise RuntimeError("AgentService not available on this runtime")

        result = await svc.send(
            message,
            session_key=params.get("session_key", "desktop:default"),
            channel=params.get("channel", "desktop"),
            chat_id=params.get("chat_id", "default"),
        )
        return result

    async def _chat_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Cancel a running execution."""
        execution_id = params.get("execution_id")
        if not execution_id:
            raise ValueError("params.execution_id is required")

        svc = self._runtime.agent_service
        if svc is None:
            raise RuntimeError("AgentService not available on this runtime")

        found = await svc.cancel(execution_id, reason=params.get("reason", "user"))
        return {"success": found, "execution_id": execution_id}

    async def _chat_regenerate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Regenerate the last assistant response by popping it and re-sending."""
        session_key = params.get("session_key", "desktop:default")
        channel = params.get("channel", "desktop")
        chat_id = params.get("chat_id", "default")

        svc = self._runtime.agent_service
        if svc is None:
            raise RuntimeError("AgentService not available on this runtime")

        session_mgr = self._runtime.session_manager
        if session_mgr is None:
            raise RuntimeError("SessionManager not available on this runtime")

        session = session_mgr.get_or_create(session_key)
        # Find the last user message to re-send
        last_user_msg = None
        for msg in reversed(session.messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break

        if not last_user_msg:
            raise ValueError("No user message found in session to regenerate")

        # Remove trailing assistant messages after the last user message
        while session.messages and session.messages[-1].get("role") != "user":
            session.messages.pop()

        session_mgr.save(session)

        result = await svc.send(
            last_user_msg,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
        )
        return result

    async def _chat_approve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Approve a pending dangerous-command approval request."""
        approval_id = params.get("approval_id")
        if not approval_id:
            raise ValueError("params.approval_id is required")

        svc = self._runtime.approval_service
        if svc is None:
            raise RuntimeError("ToolApprovalService not available on this runtime")

        from miqi.runtime.approval import ApprovalDecision
        choice = params.get("choice", "once")
        if choice not in ("once", "session", "always"):
            raise ValueError("params.choice must be 'once', 'session', or 'always'")

        decision = ApprovalDecision(choice)
        resolved = await svc.resolve(approval_id, decision)
        return {"success": resolved, "approval_id": approval_id, "decision": choice}

    async def _chat_deny(self, params: dict[str, Any]) -> dict[str, Any]:
        """Deny a pending dangerous-command approval request."""
        approval_id = params.get("approval_id")
        if not approval_id:
            raise ValueError("params.approval_id is required")

        svc = self._runtime.approval_service
        if svc is None:
            raise RuntimeError("ToolApprovalService not available on this runtime")

        from miqi.runtime.approval import ApprovalDecision
        resolved = await svc.resolve(approval_id, ApprovalDecision.DENY)
        return {"success": resolved, "approval_id": approval_id, "decision": "deny"}

    # ── Workspace handlers ────────────────────────────────────────────────

    async def _workspace_status(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        return svc.status()

    async def _workspace_list(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        return svc.list_workspaces()

    async def _workspace_open(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        path = params.get("path")
        if not path:
            raise ValueError("params.path is required")
        return svc.open_workspace(path)

    async def _workspace_index(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        return svc.index(
            subdir=params.get("subdir"),
            depth=params.get("depth", 6),
        )

    async def _workspace_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        path = params.get("path")
        if not path:
            raise ValueError("params.path is required")
        return svc.read_preview(path)

    async def _workspace_pin_file(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        path = params.get("path")
        if not path:
            raise ValueError("params.path is required")
        return svc.pin_file(path)

    async def _workspace_unpin_file(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        path = params.get("path")
        if not path:
            raise ValueError("params.path is required")
        return svc.unpin_file(path)

    async def _workspace_list_pinned(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        return svc.list_pinned()

    async def _workspace_list_recent(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.workspace_service
        if svc is None:
            raise RuntimeError("WorkspaceService not available on this runtime")
        return svc.list_recent(limit=params.get("limit", 20))

    # ── Memory handlers ──────────────────────────────────────────────────

    async def _memory_status(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        return svc.status()

    async def _memory_search(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        return svc.search(params.get("query", ""), limit=params.get("limit", 20))

    async def _memory_update(self, params: dict[str, Any]) -> dict[str, Any]:
        """Unified memory update — dispatches to remember/append_today/learn_lesson."""
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        text = params.get("text")
        if not text:
            raise ValueError("params.text is required")
        action = params.get("action", "remember")
        if action == "remember":
            result = svc.remember(
                text,
                session_key=params.get("session_key", "desktop:default"),
                source=params.get("source", "desktop"),
            )
            await self._emit_memory_changed("snapshot")
            return result
        elif action == "append_today":
            result = svc.append_today(text)
            await self._emit_memory_changed("snapshot")
            return result
        elif action == "learn_lesson":
            better_action = params.get("better_action") or text
            result = svc.learn_lesson(
                trigger=text,
                better_action=better_action,
                bad_action=params.get("bad_action", ""),
                session_key=params.get("session_key", "desktop:default"),
                source=params.get("source", "desktop"),
            )
            await self._emit_memory_changed("lesson")
            return result
        else:
            raise ValueError(f"params.action must be 'remember', 'append_today', or 'learn_lesson', got '{action}'")

    async def _memory_remember(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        text = params.get("text")
        if not text:
            raise ValueError("params.text is required")
        result = svc.remember(
            text,
            session_key=params.get("session_key", "desktop:default"),
            source=params.get("source", "desktop"),
        )
        await self._emit_memory_changed("snapshot")
        return result

    async def _memory_append_today(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        content = params.get("content")
        if not content:
            raise ValueError("params.content is required")
        result = svc.append_today(content)
        await self._emit_memory_changed("snapshot")
        return result

    async def _memory_learn_lesson(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        trigger = params.get("trigger")
        better_action = params.get("better_action")
        if not trigger:
            raise ValueError("params.trigger is required")
        if not better_action:
            raise ValueError("params.better_action is required")
        result = svc.learn_lesson(
            trigger,
            better_action,
            bad_action=params.get("bad_action", ""),
            session_key=params.get("session_key", "desktop:default"),
            source=params.get("source", "desktop"),
        )
        await self._emit_memory_changed("lesson")
        return result

    async def _memory_list_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        return svc.list_snapshot_items(
            session_key=params.get("session_key"),
            limit=params.get("limit", 50),
        )

    async def _memory_list_lessons(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        return svc.list_lessons(
            include_disabled=params.get("include_disabled", False),
            limit=params.get("limit", 50),
        )

    async def _memory_delete_snapshot_item(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        item_id = params.get("item_id")
        if not item_id:
            raise ValueError("params.item_id is required")
        result = svc.delete_snapshot_item(item_id)
        await self._emit_memory_changed("snapshot")
        return result

    async def _memory_delete_lesson(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        lesson_id = params.get("lesson_id")
        if not lesson_id:
            raise ValueError("params.lesson_id is required")
        result = svc.delete_lesson(lesson_id)
        await self._emit_memory_changed("lesson")
        return result

    async def _memory_set_lesson_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.memory_service
        if svc is None:
            raise RuntimeError("MemoryService not available on this runtime")
        lesson_id = params.get("lesson_id")
        if not lesson_id:
            raise ValueError("params.lesson_id is required")
        enabled = params.get("enabled", True)
        result = svc.set_lesson_enabled(lesson_id, enabled)
        await self._emit_memory_changed("lesson")
        return result

    # ── Cron handlers ────────────────────────────────────────────────────

    async def _cron_list(self, params: dict[str, Any]) -> dict[str, Any]:
        cron = self._runtime.cron
        include_disabled = params.get("include_disabled", False)
        jobs = cron.list_jobs(include_disabled=include_disabled)
        return {
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "at_ms": j.schedule.at_ms,
                        "every_ms": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                    },
                    "state": {
                        "next_run_at_ms": j.state.next_run_at_ms,
                        "last_run_at_ms": j.state.last_run_at_ms,
                        "last_status": j.state.last_status,
                        "last_error": j.state.last_error,
                    },
                    "created_at_ms": j.created_at_ms,
                }
                for j in jobs
            ],
            "count": len(jobs),
        }

    async def _cron_add(self, params: dict[str, Any]) -> dict[str, Any]:
        from miqi.cron.types import CronSchedule

        name = params.get("name")
        if not name:
            raise ValueError("params.name is required")

        message = params.get("message", "")
        if not message:
            raise ValueError("params.message is required")

        schedule_data = params.get("schedule", {})
        schedule = CronSchedule(
            kind=schedule_data.get("kind", "every"),
            at_ms=schedule_data.get("at_ms"),
            every_ms=schedule_data.get("every_ms"),
            expr=schedule_data.get("expr"),
            tz=schedule_data.get("tz"),
        )

        cron = self._runtime.cron
        job = cron.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=params.get("deliver", False),
            channel=params.get("channel"),
            to=params.get("to"),
            delete_after_run=params.get("delete_after_run", False),
        )
        await self._emit_cron_changed(
            job_id=job.id,
            job_name=job.name,
            action="added",
        )
        return {"success": True, "job_id": job.id}

    async def _cron_update(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("params.job_id is required")

        cron = self._runtime.cron
        job_name = ""
        if "enabled" in params:
            result = cron.enable_job(job_id, params["enabled"])
            if result is None:
                raise ValueError(f"Job '{job_id}' not found")
            job_name = result.name
            await self._emit_cron_changed(
                job_id=job_id,
                job_name=job_name,
                action="updated",
            )
        return {"success": True, "job_id": job_id}

    async def _cron_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("params.job_id is required")

        cron = self._runtime.cron
        job_name = ""
        for job in cron.list_jobs(include_disabled=True):
            if job.id == job_id:
                job_name = job.name
                break
        removed = cron.remove_job(job_id)
        if removed:
            await self._emit_cron_changed(
                job_id=job_id,
                job_name=job_name,
                action="deleted",
            )
        return {"success": removed, "job_id": job_id}

    # ── Heartbeat handlers ──────────────────────────────────────────────

    async def _heartbeat_status(self, params: dict[str, Any]) -> dict[str, Any]:
        hb = self._runtime.heartbeat_service
        if hb is None:
            return {
                "enabled": self._runtime.config.heartbeat.enabled,
                "interval_seconds": self._runtime.config.heartbeat.interval_seconds,
                "running": False,
            }
        return {
            "enabled": hb.enabled,
            "interval_seconds": hb.interval_s,
            "running": hb._running,
        }

    async def _heartbeat_update(self, params: dict[str, Any]) -> dict[str, Any]:
        from miqi.config.loader import save_config

        hb = self._runtime.heartbeat_service
        config = self._runtime.config

        if "enabled" in params:
            config.heartbeat.enabled = params["enabled"]
            if hb is not None:
                hb.enabled = params["enabled"]
        if "interval_seconds" in params:
            config.heartbeat.interval_seconds = int(params["interval_seconds"])
            if hb is not None:
                hb.interval_s = int(params["interval_seconds"])

        save_config(config)
        return {"success": True}

    # ── Context handlers ─────────────────────────────────────────────────

    async def _context_status(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.context_service
        if svc is None:
            raise RuntimeError("ContextService not available on this runtime")
        return svc.status()

    async def _context_list_bootstrap(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.context_service
        if svc is None:
            raise RuntimeError("ContextService not available on this runtime")
        return svc.list_bootstrap()

    async def _context_read_bootstrap(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.context_service
        if svc is None:
            raise RuntimeError("ContextService not available on this runtime")
        name = params.get("name")
        if not name:
            raise ValueError("params.name is required")
        return svc.read_bootstrap(name)

    async def _context_list_skills(self, params: dict[str, Any]) -> dict[str, Any]:
        svc = self._runtime.context_service
        if svc is None:
            raise RuntimeError("ContextService not available on this runtime")
        return svc.list_skills()
