"""Gateway command registration for MiQi CLI."""

from __future__ import annotations

import asyncio

import typer


def register_gateway_command(
    app: typer.Typer,
    *,
    console,
    logo: str,
    make_provider,
) -> None:
    """Register gateway command on the root app."""

    @app.command()
    def gateway(
        port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    ):
        """Start the MiQi gateway."""
        from miqi.channels.manager import ChannelManager
        from miqi.config.loader import load_config
        from miqi.heartbeat.service import HeartbeatService
        from miqi.runtime.factory import create_runtime, wire_cron_callback

        if verbose:
            import logging

            logging.basicConfig(level=logging.DEBUG)

        console.print(f"{logo} Starting MiQi gateway on port {port}...")

        config = load_config()
        rt = create_runtime(config, make_provider=make_provider, init_session_manager=True)
        wire_cron_callback(rt)

        channels = ChannelManager(config, rt.bus)

        def _pick_heartbeat_target() -> tuple[str, str]:
            enabled = set(channels.enabled_channels)
            for item in rt.session_manager.list_sessions():
                key = item.get("key") or ""
                if ":" not in key:
                    continue
                channel, chat_id = key.split(":", 1)
                if channel in {"cli", "system"}:
                    continue
                if channel in enabled and chat_id:
                    return channel, chat_id
            return "cli", "direct"

        async def on_heartbeat(prompt: str) -> str:
            channel, chat_id = _pick_heartbeat_target()

            async def _silent(*_args, **_kwargs):
                pass

            return await rt.agent.process_direct(
                prompt,
                session_key="heartbeat",
                channel=channel,
                chat_id=chat_id,
                on_progress=_silent,
            )

        async def on_heartbeat_notify(response: str) -> None:
            from miqi.bus.events import OutboundMessage

            channel, chat_id = _pick_heartbeat_target()
            if channel == "cli":
                return
            await rt.bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=response)
            )

        heartbeat_interval_s = max(1, config.heartbeat.interval_seconds)
        heartbeat = HeartbeatService(
            workspace=config.workspace_path,
            on_heartbeat=on_heartbeat,
            on_notify=on_heartbeat_notify,
            interval_s=heartbeat_interval_s,
            enabled=config.heartbeat.enabled,
        )

        if channels.enabled_channels:
            console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
        else:
            console.print("[yellow]Warning: No channels enabled[/yellow]")

        if config.heartbeat.enabled:
            if heartbeat_interval_s % 60 == 0:
                console.print(
                    f"[green]✓[/green] Heartbeat: every {heartbeat_interval_s // 60}m"
                )
            else:
                console.print(f"[green]✓[/green] Heartbeat: every {heartbeat_interval_s}s")
        else:
            console.print("[yellow]Heartbeat disabled[/yellow]")

        async def run():
            try:
                await rt.cron.start()

                cron_status = rt.cron.status()
                if cron_status["jobs"] > 0:
                    console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

                await heartbeat.start()
                await asyncio.gather(
                    rt.agent.run(),
                    channels.start_all(),
                )
            except KeyboardInterrupt:
                console.print("\nShutting down...")
            finally:
                await rt.agent.close_mcp()
                heartbeat.stop()
                rt.cron.stop()
                rt.agent.stop()
                await channels.stop_all()

        asyncio.run(run())
