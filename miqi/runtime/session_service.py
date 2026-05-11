"""SessionService — desktop-friendly wrapper over SessionManager.

Provides the session RPC methods consumed by the desktop UI without
modifying SessionManager's internal storage or CLI/gateway behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from miqi.session.manager import Session, SessionManager


class SessionService:
    """Wraps :class:`SessionManager` with desktop-oriented methods.

    All methods return plain dicts suitable for JSON-RPC responses.
    The underlying ``SessionManager`` is not modified — CLI and gateway
    continue to use it directly.
    """

    def __init__(self, session_manager: SessionManager) -> None:
        self._sm = session_manager

    # ── List ─────────────────────────────────────────────────────────────

    def list_sessions(self, *, include_archived: bool = False) -> dict[str, Any]:
        """Return session list with key/title/preview/source/updated_at/message_count."""
        raw = self._sm.list_sessions()
        items: list[dict[str, Any]] = []
        for info in raw:
            key = info["key"]
            session = self._sm.get_or_create(key)
            # Skip archived sessions unless explicitly requested
            if not include_archived and session.metadata.get("archived"):
                self._sm.invalidate(key)
                continue
            title = self._derive_title(session)
            preview = self._derive_preview(session)
            item = {
                "key": key,
                "title": title,
                "preview": preview,
                "source": self._derive_source(key),
                "updated_at": session.updated_at.isoformat(),
                "message_count": len(session.messages),
            }
            if session.metadata.get("archived"):
                item["archived"] = True
            items.append(item)
            # Evict from cache — list shouldn't warm the entire cache
            self._sm.invalidate(key)
        # Sort by actual session.updated_at descending (not stale metadata line)
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return {"sessions": items, "count": len(items)}

    # ── Create ───────────────────────────────────────────────────────────

    def create_session(self, key: str, title: str | None = None) -> dict[str, Any]:
        """Create a new session and persist it. Returns session info."""
        if not key or not key.strip():
            raise ValueError("session key must not be empty")
        stripped_title = title.strip() if title else None
        session = self._sm.get_or_create(key)
        if stripped_title:
            session.metadata["title"] = stripped_title
            # Force full rewrite so metadata title is persisted even for
            # existing sessions where save() would otherwise append-only.
            session.saved_count = len(session.messages) + 1
        self._sm.save(session)
        return {
            "key": key,
            "title": stripped_title or key,
            "preview": "",
            "source": self._derive_source(key),
            "updated_at": session.updated_at.isoformat(),
            "message_count": len(session.messages),
        }

    # ── Rename ───────────────────────────────────────────────────────────

    def rename_session(self, key: str, title: str) -> dict[str, Any]:
        """Set the title of an existing session."""
        if not key or not key.strip():
            raise ValueError("session key must not be empty")
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        session = self._sm.get_or_create(key)
        session.metadata["title"] = title.strip()
        # Force a full rewrite so updated metadata is persisted.
        # Setting saved_count higher than len(messages) triggers the
        # rewrite branch in SessionManager.save().
        session.saved_count = len(session.messages) + 1
        self._sm.save(session)
        return {"key": key, "title": title.strip()}

    # ── Archive ──────────────────────────────────────────────────────────

    def archive_session(self, key: str) -> dict[str, Any]:
        """Mark a session as archived. It is hidden from list/search by default."""
        if not key or not key.strip():
            raise ValueError("session key must not be empty")
        session = self._sm.get_or_create(key)
        session.metadata["archived"] = True
        session.saved_count = len(session.messages) + 1  # force rewrite
        self._sm.save(session)
        return {"key": key, "archived": True}

    def unarchive_session(self, key: str) -> dict[str, Any]:
        """Un-archive a session so it appears in list/search again."""
        if not key or not key.strip():
            raise ValueError("session key must not be empty")
        session = self._sm.get_or_create(key)
        session.metadata.pop("archived", None)
        session.saved_count = len(session.messages) + 1  # force rewrite
        self._sm.save(session)
        return {"key": key, "archived": False}

    # ── Delete ───────────────────────────────────────────────────────────

    def delete_session(self, key: str) -> dict[str, Any]:
        """Delete a session by key."""
        if not key or not key.strip():
            raise ValueError("session key must not be empty")
        deleted = self._sm.delete(key)
        return {"key": key, "deleted": deleted}

    # ── Search ───────────────────────────────────────────────────────────

    def search_sessions(self, query: str, *, include_archived: bool = False) -> dict[str, Any]:
        """Simple substring search over session titles and message content.

        This scans JSONL files on disk — adequate for small-to-medium
        session counts.  SQLite/FTS5 can replace this later without
        changing the IPC response shape.
        """
        if not query or not query.strip():
            return {"sessions": [], "count": 0, "query": query}

        q_lower = query.strip().lower()
        results: list[dict[str, Any]] = []

        for info in self._sm.list_sessions():
            key = info["key"]
            session = self._sm.get_or_create(key)
            # Skip archived unless explicitly requested
            if not include_archived and session.metadata.get("archived"):
                self._sm.invalidate(key)
                continue
            # Check title (from already-loaded session metadata)
            title = session.metadata.get("title")
            if title and q_lower in title.lower():
                results.append(self._session_info(key, info, title))
                self._sm.invalidate(key)
                continue
            # Check message content
            if self._jsonl_contains(key, q_lower):
                results.append(self._session_info(key, info, title))
            self._sm.invalidate(key)

        results.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"sessions": results, "count": len(results), "query": query.strip()}

    # ── Load ─────────────────────────────────────────────────────────────

    def load_session(self, key: str) -> dict[str, Any]:
        """Return full message list for a session.

        Internal objects (reasoning_content, raw tool objects) are stripped
        to keep the response safe for IPC transit.
        """
        if not key or not key.strip():
            raise ValueError("session key must not be empty")
        session = self._sm.get_or_create(key)
        messages = self._sanitize_messages(session.messages)
        title = session.metadata.get("title", "")
        return {
            "key": key,
            "title": title or key,
            "source": self._derive_source(key),
            "updated_at": session.updated_at.isoformat(),
            "message_count": len(session.messages),
            "messages": messages,
        }

    # ── Private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _derive_title(session: Session) -> str:
        title = session.metadata.get("title")
        if title:
            return title
        # Fallback: use first user message as title (truncated)
        for msg in session.messages:
            if msg.get("role") == "user" and msg.get("content"):
                text = str(msg["content"])
                return text[:80] + ("…" if len(text) > 80 else "")
        return session.key

    @staticmethod
    def _derive_preview(session: Session) -> str:
        for msg in reversed(session.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                text = str(msg["content"])
                return text[:120] + ("…" if len(text) > 120 else "")
        for msg in reversed(session.messages):
            if msg.get("role") == "user" and msg.get("content"):
                text = str(msg["content"])
                return text[:120] + ("…" if len(text) > 120 else "")
        return ""

    @staticmethod
    def _derive_source(key: str) -> str:
        if ":" not in key:
            return "unknown"
        channel = key.split(":", 1)[0]
        return channel

    def _session_info(self, key: str, raw_info: dict, title: str | None) -> dict[str, Any]:
        session = self._sm.get_or_create(key)
        info = {
            "key": key,
            "title": title or self._derive_title(session),
            "preview": self._derive_preview(session),
            "source": self._derive_source(key),
            "updated_at": session.updated_at.isoformat(),
            "message_count": len(session.messages),
        }
        if session.metadata.get("archived"):
            info["archived"] = True
        self._sm.invalidate(key)
        return info

    def _read_title_from_disk(self, key: str) -> str | None:
        """Read just the metadata line from a JSONL file to get the title."""
        path = self._sm._get_session_path(key)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline().strip()
                if not first_line:
                    return None
                data = json.loads(first_line)
                if data.get("_type") == "metadata":
                    return data.get("metadata", {}).get("title")
        except Exception:
            pass
        return None

    def _jsonl_contains(self, key: str, query_lower: str) -> bool:
        """Check if a session's message content matches the query.

        Parses JSONL and only searches user-visible text fields
        (message content), avoiding false positives from JSON keys.
        """
        path = self._sm._get_session_path(key)
        if not path.exists():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("_type") == "metadata":
                        continue
                    # Only search message content — not role, tool_call_id, etc.
                    content = data.get("content")
                    if isinstance(content, str) and query_lower in content.lower():
                        return True
        except Exception:
            pass
        return False

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip internal fields from messages for safe IPC transit."""
        safe: list[dict[str, Any]] = []
        allowed_keys = {"role", "content", "tool_calls", "tool_call_id", "name", "timestamp"}
        for msg in messages:
            entry: dict[str, Any] = {}
            for k in allowed_keys:
                if k in msg:
                    entry[k] = msg[k]
            safe.append(entry)
        return safe
