"""P-H-E 循环编排器 — 管理 PiEvo 会话生命周期"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PHESession:
    """一次 PiEvo 发现会话的状态"""
    session_id: str
    task: str
    max_rounds: int
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    current_round: int = 0
    current_phase: str = "idle"
    status: str = "pending"  # pending | running | completed | failed | stopped
    principles: dict = field(default_factory=dict)
    beliefs: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    best_result: dict | None = None
    error: str | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)


class PHEOrchestrator:
    """管理 PiEvo 会话的创建、查询和终止"""

    def __init__(self, engine_factory, max_concurrent: int = 3):
        self._engine_factory = engine_factory  # () -> PiEvoEngine
        self._sessions: dict[str, PHESession] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def start_session(
        self,
        session_id: str,
        task: str,
        max_rounds: int = 20,
        available_tools: list | None = None,
    ) -> PHESession:
        session = PHESession(
            session_id=session_id,
            task=task,
            max_rounds=max_rounds,
            status="running",
        )
        self._sessions[session_id] = session

        async def _run():
            async with self._semaphore:
                try:
                    engine = self._engine_factory()
                    engine.task = task

                    async def _on_progress(p):
                        session.current_phase = p["phase"]
                        session.current_round = p["round"]

                    async def _on_round_complete(r, result):
                        session.principles = result.get("principles", {})
                        session.beliefs = result.get("beliefs", {})
                        session.history.append(result.get("outcome"))
                        session.current_round = r

                    engine.on_progress = _on_progress
                    engine.on_round_complete = _on_round_complete

                    report = await engine.run(
                        max_rounds=max_rounds,
                        available_tools=available_tools,
                    )
                    session.best_result = report.get("best_result")
                    session.principles = report.get("principles", {})
                    session.beliefs = report.get("beliefs", {})
                    session.status = "completed"
                    session.current_phase = "done"
                except asyncio.CancelledError:
                    session.status = "stopped"
                    session.current_phase = "cancelled"
                except Exception as e:
                    logger.error(f"PiEvo session {session_id} failed: {e}")
                    session.status = "failed"
                    session.error = str(e)

        session._task = asyncio.create_task(_run())
        return session

    def get_session(self, session_id: str) -> PHESession | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> dict[str, PHESession]:
        return dict(self._sessions)

    def stop_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session and session._task and not session._task.done():
            session._task.cancel()
            return True
        return False
