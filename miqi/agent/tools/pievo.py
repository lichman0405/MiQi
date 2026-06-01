"""PiEvo 工具 — 触发和管理自主科学发现会话"""
import json
import logging

from miqi.agent.tools.base import Tool

logger = logging.getLogger(__name__)


class PievoRunTool(Tool):
    """Start a PiEvo autonomous discovery session."""

    def __init__(self, orchestrator, workspace: str = ""):
        self._orchestrator = orchestrator
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "pievo_run"

    @property
    def description(self) -> str:
        return (
            "Start a PiEvo autonomous discovery session. PiEvo runs a "
            "Principle-Hypothesis-Experiment (P-H-E) loop using Bayesian "
            "optimization + Information-Directed Sampling (IDS) to explore "
            "principle space, generate hypotheses, and execute experiments. "
            "Use when the user wants to optimize experimental parameters, "
            "discover scientific principles, or run multi-round automated "
            "experiments. Requires scientific MCP tools to be available."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Natural language description of the scientific discovery task, including parameter space, optimization targets, and constraints.",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of MCP tool names to use for experiments (e.g., ['mcp_zeopp-backend_analyze']).",
                },
                "max_rounds": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of P-H-E rounds to run.",
                },
                "warm_up_rounds": {
                    "type": "integer",
                    "default": 5,
                    "description": "Number of warm-up rounds using uncertainty sampling before IDS activates.",
                },
            },
            "required": ["task", "tools"],
        }

    async def execute(
        self,
        task: str,
        tools: list,
        max_rounds: int = 20,
        warm_up_rounds: int = 5,
    ) -> str:
        import time as _time
        session_id = f"pievo_{int(_time.time())}"

        session = await self._orchestrator.start_session(
            session_id=session_id,
            task=task,
            max_rounds=max_rounds,
            available_tools=tools,
        )

        return json.dumps({
            "session_id": session_id,
            "status": session.status,
            "message": (
                f"PiEvo discovery session started (max {max_rounds} rounds). "
                f"Use pievo_status('{session_id}') to check progress."
            ),
        }, ensure_ascii=False)


class PievoStatusTool(Tool):
    """Check the status and results of a PiEvo session."""

    def __init__(self, orchestrator):
        self._orchestrator = orchestrator

    @property
    def name(self) -> str:
        return "pievo_status"

    @property
    def description(self) -> str:
        return "Check the current status and results of a PiEvo discovery session."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID returned by pievo_run.",
                },
            },
            "required": ["session_id"],
        }

    async def execute(self, session_id: str) -> str:
        session = self._orchestrator.get_session(session_id)
        if not session:
            return json.dumps({"error": f"Session '{session_id}' not found."})

        return json.dumps({
            "session_id": session.session_id,
            "status": session.status,
            "current_round": session.current_round,
            "current_phase": session.current_phase,
            "max_rounds": session.max_rounds,
            "principles_count": len(session.principles),
            "best_result": session.best_result,
            "error": session.error,
        }, ensure_ascii=False, default=str)


class PievoStopTool(Tool):
    """Stop a running PiEvo session."""

    def __init__(self, orchestrator):
        self._orchestrator = orchestrator

    @property
    def name(self) -> str:
        return "pievo_stop"

    @property
    def description(self) -> str:
        return "Stop a running PiEvo discovery session."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to stop.",
                },
            },
            "required": ["session_id"],
        }

    async def execute(self, session_id: str) -> str:
        stopped = self._orchestrator.stop_session(session_id)
        return json.dumps({
            "session_id": session_id,
            "stopped": stopped,
            "message": "Session stopped." if stopped else "Session not found or already finished.",
        }, ensure_ascii=False)
