"""WorkspaceService — desktop-friendly workspace/file operations.

Provides the workspace RPC methods consumed by the desktop UI.
Enforces path restrictions, applies ignore rules, and protects
against binary/large-file reads.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger

from miqi.agent.tools.filesystem import _resolve_path

if TYPE_CHECKING:
    from miqi.agent.loop import AgentLoop

# ── Constants ───────────────────────────────────────────────────────────

DEFAULT_IGNORE_PATTERNS: list[str] = [
    ".git",
    ".miqi",
    "sessions",
    "memory",
    "data",
    "__pycache__",
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
    ".DS_Store",
    "Thumbs.db",
]

PREVIEW_MAX_CHARS = 4096
PREVIEW_MAX_BYTES = 1 << 20  # 1 MiB
INDEX_MAX_DEPTH = 6
INDEX_MAX_ENTRIES = 2000

# Binary sniff: read first 8 KB and check for NUL / high control-byte ratio
_BINARY_SNIFF_SIZE = 8192
_BINARY_SNIFF_THRESHOLD = 0.30  # >30% NUL or control bytes => binary

# Heuristics for binary detection by extension
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".mp3", ".mp4", ".wav", ".avi", ".mkv", ".mov", ".flac",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".pyd",
    ".wasm", ".class", ".jar", ".war",
    ".sqlite", ".db", ".parquet", ".feather",
})


def _is_binary_path(path: Path) -> bool:
    return path.suffix.lower() in _BINARY_EXTENSIONS


def _sniff_binary(path: Path) -> bool:
    """Sniff the first few KB for NUL or high control-byte ratio."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(_BINARY_SNIFF_SIZE)
    except (OSError, PermissionError):
        return True
    if not chunk:
        return False
    control = sum(1 for b in chunk if b == 0 or (b < 32 and b not in (9, 10, 13)))
    return (control / len(chunk)) > _BINARY_SNIFF_THRESHOLD


def _meta_dir_for_root(root: Path) -> Path:
    """Return the MiQi data directory for workspace metadata.

    Layout: ``<data_root>/desktop/workspaces/<sha256_hex>[:16].json``
    Falls back to ``~/.miqi/desktop/workspaces/`` if data dir is unavailable.
    """
    from miqi.config.loader import get_data_dir

    data = get_data_dir()
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return data / "desktop" / "workspaces" / f"{digest}.json"


class WorkspaceService:
    """Workspace and file-tree operations for the desktop UI.

    All methods return plain dicts suitable for JSON-RPC responses.
    Path arguments are resolved against the project root and must
    stay within it — ``_resolve_path`` enforces this.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        agent: AgentLoop | None = None,
        ignore_patterns: list[str] | None = None,
        restrict_to_workspace: bool = True,
    ) -> None:
        self._root = project_root.resolve()
        self._agent = agent
        self._ignore = list(ignore_patterns) if ignore_patterns else list(DEFAULT_IGNORE_PATTERNS)
        self._restrict = restrict_to_workspace

        # Persisted metadata — stored under MiQi data root, NOT project root
        self._meta_path = _meta_dir_for_root(self._root)
        self._pinned: list[str] = []
        self._recent: list[str] = []
        self._workspace_history: list[str] = [str(self._root)]
        self._load_meta()

    # ── Public RPC methods ───────────────────────────────────────────────

    @staticmethod
    def _rel(resolved: Path, root: Path) -> str:
        """Return a relative path using forward slashes."""
        return str(resolved.relative_to(root)).replace("\\", "/")

    def status(self) -> dict[str, Any]:
        """Return current workspace info."""
        return {
            "project_root": str(self._root),
            "exists": self._root.is_dir(),
            "restrict_to_workspace": self._restrict,
            "pinned_count": len(self._pinned),
            "recent_count": len(self._recent),
        }

    def list_workspaces(self) -> dict[str, Any]:
        """Return known workspaces."""
        items = []
        seen = set()
        # Current root first
        items.append({
            "path": str(self._root),
            "is_current": True,
            "exists": self._root.is_dir(),
        })
        seen.add(str(self._root))
        # History (most recent first)
        for ws in reversed(self._workspace_history):
            if ws in seen:
                continue
            seen.add(ws)
            items.append({
                "path": ws,
                "is_current": False,
                "exists": Path(ws).is_dir(),
            })
        return {"workspaces": items, "count": len(items)}

    def open_workspace(self, path: str) -> dict[str, Any]:
        """Switch to a different project root (must exist).

        Also updates the AgentLoop and all its tools to use the new root,
        while keeping sessions/memory/cron in the MiQi data root.
        """
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError(f"directory does not exist: {path}")
        self._root = candidate

        # Sync the agent runtime
        if self._agent is not None:
            self._agent.set_workspace(self._root)

        # Reset per-workspace state and load meta for the new root
        self._meta_path = _meta_dir_for_root(self._root)
        self._pinned.clear()
        self._recent.clear()
        self._load_meta()

        # Record in history
        ws_str = str(self._root)
        if ws_str in self._workspace_history:
            self._workspace_history.remove(ws_str)
        self._workspace_history.append(ws_str)
        self._save_meta()

        return self.status()

    def index(self, *, subdir: str | None = None, depth: int = INDEX_MAX_DEPTH) -> dict[str, Any]:
        """Return file-tree entries under the project root (or subdir).

        Each entry has: name, path (relative), is_dir, is_symlink, size, modified.
        Respects ignore patterns and caps total entries.
        In restricted mode, symlinks are skipped entirely.
        """
        base = self._resolve_subdir(subdir)
        entries: list[dict[str, Any]] = []
        self._walk(base, entries, depth=depth, max_entries=INDEX_MAX_ENTRIES)
        return {
            "root": str(self._root),
            "subdir": subdir or "",
            "entries": entries,
            "count": len(entries),
            "truncated": len(entries) >= INDEX_MAX_ENTRIES,
        }

    def read_preview(self, path: str) -> dict[str, Any]:
        """Return a text preview of a file, with safety guards.

        Returns a dict with: path, exists, is_dir, is_binary, size,
        truncated, content (text only, max PREVIEW_MAX_CHARS).
        Binary detection uses extension heuristics *and* content sniffing.
        """
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"path": path, "exists": False}
        if resolved.is_dir():
            return {"path": path, "exists": True, "is_dir": True}

        stat = resolved.stat()
        size = stat.st_size
        is_binary = _is_binary_path(resolved)

        result: dict[str, Any] = {
            "path": path,
            "exists": True,
            "is_dir": False,
            "is_binary": is_binary,
            "size": size,
            "truncated": False,
        }

        if is_binary:
            result["content"] = None
            return result

        if size > PREVIEW_MAX_BYTES:
            # Still sniff — even large files might not be binary
            result["truncated"] = True
            if _sniff_binary(resolved):
                result["is_binary"] = True
                result["content"] = None
            else:
                result["content"] = None
            return result

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception:
            result["is_binary"] = True
            result["content"] = None
            return result

        # Content sniff for files that passed extension check
        if _sniff_binary(resolved):
            result["is_binary"] = True
            result["content"] = None
            return result

        if len(text) > PREVIEW_MAX_CHARS:
            text = text[:PREVIEW_MAX_CHARS]
            result["truncated"] = True

        result["content"] = text
        return result

    def list_pinned(self) -> dict[str, Any]:
        """Return the list of pinned file paths."""
        entries = [self._file_info(p) for p in self._pinned]
        return {"files": entries, "count": len(entries)}

    def pin_file(self, path: str) -> dict[str, Any]:
        """Pin a file to the workspace context."""
        resolved = self._resolve_path(path)
        if not resolved.exists() or not resolved.is_file():
            raise ValueError(f"file does not exist: {path}")
        rel = self._rel(resolved, self._root)
        if rel not in self._pinned:
            self._pinned.append(rel)
            self._save_meta()
        return {"path": rel, "pinned": True}

    def unpin_file(self, path: str) -> dict[str, Any]:
        """Remove a file from the pinned list."""
        resolved = self._resolve_path(path)
        rel = self._rel(resolved, self._root)
        if rel in self._pinned:
            self._pinned.remove(rel)
            self._save_meta()
        return {"path": rel, "pinned": False}

    def list_recent(self, limit: int = 20) -> dict[str, Any]:
        """Return recently agent-touched files."""
        entries = [self._file_info(p) for p in self._recent[-limit:]]
        entries.reverse()
        return {"files": entries, "count": len(entries)}

    def touch_recent(self, path: str) -> dict[str, Any]:
        """Mark a file as recently accessed (by agent tools)."""
        resolved = self._resolve_path(path)
        rel = self._rel(resolved, self._root)
        if rel in self._recent:
            self._recent.remove(rel)
        self._recent.append(rel)
        # Cap recent list
        if len(self._recent) > 100:
            self._recent = self._recent[-100:]
        self._save_meta()
        return {"path": rel, "recorded": True}

    # ── Private helpers ──────────────────────────────────────────────────

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path within project root, enforcing restrictions."""
        return _resolve_path(
            path,
            workspace=self._root,
            allowed_dir=self._root if self._restrict else None,
        )

    def _resolve_subdir(self, subdir: str | None) -> Path:
        if not subdir:
            return self._root
        resolved = self._resolve_path(subdir)
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {subdir}")
        return resolved

    def _is_ignored(self, name: str) -> bool:
        for pattern in self._ignore:
            if fnmatch(name, pattern):
                return True
        return False

    def _walk(
        self,
        directory: Path,
        entries: list[dict[str, Any]],
        *,
        depth: int,
        max_entries: int,
        prefix: str = "",
    ) -> None:
        if depth <= 0 or len(entries) >= max_entries:
            return
        try:
            items = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(follow_symlinks=False), p.name.lower()))
        except PermissionError:
            return

        for item in items:
            if len(entries) >= max_entries:
                return
            if self._is_ignored(item.name):
                continue

            is_link = item.is_symlink()
            # Restricted mode: skip symlinks entirely to prevent traversal
            if self._restrict and is_link:
                continue

            rel = f"{prefix}{item.name}" if prefix else item.name
            try:
                lstat = item.lstat()
            except (OSError, PermissionError):
                continue

            is_dir = item.is_dir(follow_symlinks=False)
            entries.append({
                "name": item.name,
                "path": rel,
                "is_dir": is_dir,
                "is_symlink": is_link,
                "size": lstat.st_size if not is_dir else 0,
                "modified": datetime.fromtimestamp(lstat.st_mtime).isoformat(),
            })
            # Never recurse into symlinks (even in non-restricted mode)
            if is_dir and not is_link:
                self._walk(
                    item,
                    entries,
                    depth=depth - 1,
                    max_entries=max_entries,
                    prefix=rel + "/",
                )

    def _file_info(self, rel_path: str) -> dict[str, Any]:
        full = self._root / rel_path
        exists = full.exists()
        is_dir = full.is_dir() if exists else False
        is_link = full.is_symlink() if exists else False
        size = 0
        modified = ""
        if exists:
            try:
                lstat = full.lstat()
                size = lstat.st_size if not is_dir else 0
                modified = datetime.fromtimestamp(lstat.st_mtime).isoformat()
            except (OSError, PermissionError):
                pass
        return {
            "path": rel_path,
            "exists": exists,
            "is_dir": is_dir,
            "is_symlink": is_link,
            "is_binary": _is_binary_path(full) if exists else False,
            "size": size,
            "modified": modified,
        }

    def _load_meta(self) -> None:
        if not self._meta_path.exists():
            return
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
            self._pinned = self._validate_paths(data.get("pinned", []))
            self._recent = self._validate_paths(data.get("recent", []))
            self._workspace_history = data.get("workspace_history", [str(self._root)])
        except Exception:
            logger.warning("Failed to load workspace meta from {}", self._meta_path)

    def _validate_paths(self, paths: list[str]) -> list[str]:
        """Filter out paths that are no longer within the project root."""
        valid = []
        for p in paths:
            try:
                resolved = (self._root / p).resolve()
                resolved.relative_to(self._root)
                valid.append(p)
            except (ValueError, OSError):
                pass
        return valid

    def _save_meta(self) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pinned": self._pinned,
            "recent": self._recent,
            "workspace_history": self._workspace_history,
        }
        self._meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self._meta_path.chmod(0o600)
        except OSError:
            pass
