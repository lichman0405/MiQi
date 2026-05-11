"""Shared runtime factory — single source of truth for constructing the MiQi
runtime components used by ``miqi agent``, ``miqi gateway``, and the future
``miqi desktop-backend``.

Every entry-point calls :func:`create_runtime` (or the more granular helpers)
instead of hand-assembling the same 17+ ``AgentLoop`` kwargs in multiple
places.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from miqi.agent.loop import AgentLoop
    from miqi.bus.queue import MessageBus
    from miqi.config.schema import Config
    from miqi.cron.service import CronService
    from miqi.events.emitter import EventEmitter
    from miqi.providers.base import LLMProvider
    from miqi.runtime.agent_service import AgentService
    from miqi.runtime.approval import ToolApprovalService
    from miqi.session.manager import SessionManager


@dataclass
class Runtime:
    """Holds all constructed runtime components."""

    config: Config
    bus: MessageBus
    provider: LLMProvider
    agent: AgentLoop
    cron: CronService
    session_manager: Optional[SessionManager] = None
    events: EventEmitter = None  # type: ignore[assignment]
    agent_service: Optional[AgentService] = None
    approval_service: Optional["ToolApprovalService"] = None
    session_service: Optional[Any] = None  # SessionService, typed as Any to avoid import cycle
    workspace_service: Optional[Any] = None  # WorkspaceService, typed as Any to avoid import cycle
    memory_service: Optional[Any] = None  # MemoryService, typed as Any to avoid import cycle
    context_service: Optional[Any] = None  # ContextService, typed as Any to avoid import cycle
    heartbeat_service: Optional[Any] = None  # HeartbeatService, typed as Any to avoid import cycle


def create_runtime(
    config: Config,
    *,
    make_provider: Callable[[Config], LLMProvider],
    init_session_manager: bool = False,
    enable_desktop_approval: bool = False,
) -> Runtime:
    """Build the full runtime stack from a loaded ``Config``.

    Parameters
    ----------
    config:
        Loaded MiQi configuration.
    make_provider:
        Factory callable ``(Config) -> BaseProvider``.  The CLI and gateway
        each supply their own (which may print to the console on missing key).
    init_session_manager:
        When *True* a :class:`SessionManager` is constructed and wired into
        the ``AgentLoop``.  The gateway always needs this; the CLI does
        not (it relies on the ``AgentLoop``'s built-in session handling).
    enable_desktop_approval:
        When *True*, create a :class:`ToolApprovalService` and wire an
        async ``approval_fn`` into ``ExecTool`` so that dangerous commands
        emit ``ApprovalRequested`` events and block until resolved via IPC.
        CLI and gateway leave this ``False`` — they continue using the
        synchronous ``command_approval`` CLI prompt or auto-approve path.
    """
    from miqi.agent.loop import AgentLoop
    from miqi.bus.queue import MessageBus
    from miqi.config.loader import get_data_dir
    from miqi.cron.service import CronService
    from miqi.events.emitter import EventEmitter
    from miqi.runtime.agent_service import AgentService

    bus = MessageBus()
    provider = make_provider(config)
    events = EventEmitter()

    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, job_timeout=config.cron.job_timeout_seconds)

    session_manager: SessionManager | None = None
    if init_session_manager:
        from miqi.session.manager import SessionManager

        session_manager = SessionManager(
            config.workspace_path,
            compact_threshold_messages=config.agents.sessions.compact_threshold_messages,
            compact_threshold_bytes=config.agents.sessions.compact_threshold_bytes,
            compact_keep_messages=config.agents.sessions.compact_keep_messages,
        )

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        agent_name=config.agents.defaults.name,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        reflect_after_tool_calls=config.agents.defaults.reflect_after_tool_calls,
        web_config=config.tools.web,
        paper_config=config.tools.papers,
        memory_window=config.agents.defaults.memory_window,
        max_tool_result_chars=config.agents.defaults.max_tool_result_chars,
        context_limit_chars=config.agents.defaults.context_limit_chars,
        exec_config=config.tools.exec,
        memory_config=config.agents.memory,
        self_improvement_config=config.agents.self_improvement,
        session_config=config.agents.sessions,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        session_manager=session_manager,
    )

    approval_service = None
    if enable_desktop_approval:
        from miqi.runtime.approval import ToolApprovalService

        approval_service = ToolApprovalService(
            events,
            timeout=float(config.agents.command_approval.timeout),
        )

    # Load permanent allowlist from config into command_approval module
    if config.agents.command_approval.allowlist:
        from miqi.agent.command_approval import load_permanent_allowlist
        load_permanent_allowlist(set(config.agents.command_approval.allowlist))

    agent_service = AgentService(agent, events, approval_service=approval_service)

    session_service = None
    if session_manager is not None:
        from miqi.runtime.session_service import SessionService
        session_service = SessionService(session_manager)

    workspace_service = None
    if init_session_manager:
        from miqi.runtime.workspace_service import WorkspaceService
        workspace_service = WorkspaceService(
            config.workspace_path,
            agent=agent,
            restrict_to_workspace=config.tools.restrict_to_workspace,
        )

    memory_service = None
    context_service = None
    heartbeat_service = None
    if init_session_manager:
        from miqi.runtime.memory_service import MemoryService
        from miqi.runtime.context_service import ContextService
        memory_service = MemoryService(agent.memory)
        context_service = ContextService(
            agent.context,
            agent.memory,
            workspace_service=workspace_service,
            context_limit_chars=config.agents.defaults.context_limit_chars,
        )

        # Heartbeat service for desktop runtime
        from miqi.heartbeat.service import HeartbeatService
        heartbeat_service = HeartbeatService(
            workspace=config.workspace_path,
            interval_s=config.heartbeat.interval_seconds,
            enabled=config.heartbeat.enabled,
        )

    return Runtime(
        config=config,
        bus=bus,
        provider=provider,
        agent=agent,
        cron=cron,
        session_manager=session_manager,
        events=events,
        agent_service=agent_service,
        approval_service=approval_service,
        session_service=session_service,
        workspace_service=workspace_service,
        memory_service=memory_service,
        context_service=context_service,
        heartbeat_service=heartbeat_service,
    )


def wire_cron_callback(runtime: Runtime) -> None:
    """Attach the standard cron-on-job callback to ``runtime.cron``.

    The callback calls ``agent.process_direct`` and optionally publishes
    an outbound message when ``job.payload.deliver`` is set.  Both
    ``miqi agent`` and ``miqi gateway`` use this same wiring.
    """
    from miqi.bus.events import OutboundMessage

    async def on_cron_job(job) -> str | None:
        response = await runtime.agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        if job.payload.deliver and job.payload.to:
            await runtime.bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response or "",
                )
            )
        return response

    runtime.cron.on_job = on_cron_job
