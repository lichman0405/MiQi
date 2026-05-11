"""Desktop backend command — starts the IPC layer over stdio."""

from __future__ import annotations

import asyncio

import typer


def register_desktop_command(
    app: typer.Typer,
    *,
    make_provider,
) -> None:
    """Register the desktop-backend command on the root app."""

    @app.command("desktop-backend")
    def desktop_backend(
        stdio: bool = typer.Option(True, "--stdio/--no-stdio", help="Use stdio transport"),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
    ):
        """Start the MiQi desktop backend (IPC layer for Tauri sidecar)."""
        from loguru import logger

        from miqi.config.loader import load_config
        from miqi.ipc.handlers import RpcDispatcher
        from miqi.ipc.transport import read_requests
        from miqi.runtime.factory import create_runtime, wire_cron_callback

        if not stdio:
            logger.warning("--no-stdio is not yet supported; defaulting to stdio")

        config = load_config()
        rt = create_runtime(config, make_provider=make_provider, init_session_manager=True, enable_desktop_approval=True)
        wire_cron_callback(rt)

        dispatcher = RpcDispatcher(rt)

        if verbose:
            logger.enable("miqi")
        else:
            logger.disable("miqi")

        async def run():
            await rt.cron.start()
            try:
                await read_requests(dispatcher, event_emitter=rt.events)
            finally:
                rt.agent.stop()
                rt.cron.stop()
                await rt.agent.close_mcp()

        asyncio.run(run())
