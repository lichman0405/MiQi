"""ContextService — desktop context inspection for the agent's prompt.

Provides read-only insight into what the agent sees: bootstrap files,
skills, memory hits, pinned files, and context budget/compression state.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any

from miqi.agent.context import ContextBuilder
from miqi.agent.memory import MemoryStore
from miqi.runtime.workspace_service import WorkspaceService


def _read_system_template(name: str) -> str | None:
    """Read a system-maintained bootstrap file from the package templates."""
    try:
        text = (
            importlib.resources.files("miqi.templates")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
        return text if text.strip() else None
    except Exception:
        return None


class ContextService:
    """Read-only context inspection for the desktop UI.

    Does not modify any state — only aggregates information already
    available from ContextBuilder, MemoryStore, and WorkspaceService.
    """

    def __init__(
        self,
        context_builder: ContextBuilder,
        memory_store: MemoryStore,
        workspace_service: WorkspaceService | None = None,
        *,
        context_limit_chars: int = 600_000,
    ) -> None:
        self._ctx = context_builder
        self._ms = memory_store
        self._ws = workspace_service
        self._context_limit = context_limit_chars

    # ── Status ──────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return context overview: bootstrap files, skills, memory, budget."""
        return {
            "workspace": str(self._ctx.workspace),
            "bootstrap_files": self._bootstrap_status(),
            "skills": self._skills_status(),
            "memory": self._memory_summary(),
            "pinned_files": self._pinned_summary(),
            "budget": self._budget_status(),
        }

    # ── Bootstrap ───────────────────────────────────────────────────────

    def list_bootstrap(self) -> dict[str, Any]:
        """List bootstrap files and their load status."""
        return {"files": self._bootstrap_status(), "count": len(self._bootstrap_status())}

    def read_bootstrap(self, name: str) -> dict[str, Any]:
        """Read a specific bootstrap file by name (e.g. 'AGENTS.md').

        Mirrors ContextBuilder._load_bootstrap_files logic:
        - System files (TOOLS.md): package template + optional workspace override
        - Other files: workspace only
        """
        if name not in ContextBuilder.BOOTSTRAP_FILES:
            raise ValueError(f"unknown bootstrap file: {name}")

        workspace = self._ctx.workspace
        ws_path = workspace / name
        ws_exists = ws_path.exists()
        ws_content = ""
        ws_size = 0
        if ws_exists:
            try:
                ws_content = ws_path.read_text(encoding="utf-8")
                ws_size = len(ws_content)
            except Exception:
                ws_content = ""
                ws_size = 0

        # System-maintained files: always have a package template
        if name in ContextBuilder.SYSTEM_BOOTSTRAP_FILES:
            sys_content = _read_system_template(name)
            if sys_content is not None:
                # Combine system + workspace (same logic as ContextBuilder)
                sections = [sys_content]
                if ws_exists and ws_content.strip():
                    if ws_content.strip() != sys_content.strip():
                        sections.append(f"## {name} (workspace overrides)\n\n{ws_content}")
                combined = "\n\n".join(sections)
                return {
                    "name": name,
                    "exists": True,
                    "source": "system",
                    "has_workspace_override": ws_exists and ws_content.strip() != "" and ws_content.strip() != sys_content.strip(),
                    "size": len(combined),
                    "content": combined[:8192],
                    "truncated": len(combined) > 8192,
                }

        # Non-system files: workspace only
        if ws_exists:
            return {
                "name": name,
                "exists": True,
                "source": "workspace",
                "has_workspace_override": False,
                "size": ws_size,
                "content": ws_content[:8192],
                "truncated": ws_size > 8192,
            }

        return {
            "name": name,
            "exists": False,
            "source": "none",
            "has_workspace_override": False,
            "size": 0,
            "content": None,
            "truncated": False,
        }

    # ── Skills ──────────────────────────────────────────────────────────

    def list_skills(self) -> dict[str, Any]:
        """List available skills."""
        skills = self._ctx.skills.list_skills(filter_unavailable=False)
        return {"skills": skills, "count": len(skills)}

    # ── Budget ──────────────────────────────────────────────────────────

    def _budget_status(self) -> dict[str, Any]:
        return {
            "context_limit_chars": self._context_limit,
            "estimated_usage": self._estimate_context_chars(),
        }

    # ── Private ─────────────────────────────────────────────────────────

    def _bootstrap_status(self) -> list[dict[str, Any]]:
        """List bootstrap files, mirroring ContextBuilder load rules."""
        workspace = self._ctx.workspace
        files = []
        for name in ContextBuilder.BOOTSTRAP_FILES:
            ws_path = workspace / name
            ws_exists = ws_path.exists()

            if name in ContextBuilder.SYSTEM_BOOTSTRAP_FILES:
                sys_content = _read_system_template(name)
                system_exists = sys_content is not None
                # Total effective size: system template + workspace override if present
                size = len(sys_content) if sys_content else 0
                has_ws_override = False
                if ws_exists:
                    try:
                        ws_text = ws_path.read_text(encoding="utf-8")
                        if ws_text.strip() and (sys_content is None or ws_text.strip() != sys_content.strip()):
                            size += len(f"\n\n## {name} (workspace overrides)\n\n{ws_text}")
                            has_ws_override = True
                    except Exception:
                        pass
                files.append({
                    "name": name,
                    "exists": system_exists or ws_exists,
                    "source": "system" if system_exists else ("workspace" if ws_exists else "none"),
                    "has_workspace_override": has_ws_override,
                    "size": size,
                })
            else:
                size = 0
                if ws_exists:
                    try:
                        size = ws_path.stat().st_size
                    except OSError:
                        pass
                files.append({
                    "name": name,
                    "exists": ws_exists,
                    "source": "workspace" if ws_exists else "none",
                    "has_workspace_override": False,
                    "size": size,
                })
        return files

    def _skills_status(self) -> list[dict[str, str]]:
        return self._ctx.skills.list_skills(filter_unavailable=False)

    def _memory_summary(self) -> dict[str, Any]:
        status = self._ms.get_status()
        return {
            "ltm_items": status.get("ltm_items", 0),
            "lessons_count": status.get("lessons_count", 0),
            "self_improvement_enabled": status.get("self_improvement_enabled", False),
            "snapshot_exists": status.get("snapshot_exists", False),
        }

    def _pinned_summary(self) -> dict[str, Any]:
        if self._ws is None:
            return {"count": 0, "files": []}
        pinned = self._ws.list_pinned()
        return {"count": pinned["count"], "files": [f["path"] for f in pinned["files"]]}

    def _estimate_context_chars(self) -> int:
        """Rough estimate of current context size from bootstrap + memory."""
        total = 0
        for f in self._bootstrap_status():
            total += f.get("size", 0)
        # Memory context
        try:
            mem_ctx = self._ms.get_memory_context(session_key="desktop:default", current_message="")
            total += len(mem_ctx)
        except Exception:
            pass
        return total
