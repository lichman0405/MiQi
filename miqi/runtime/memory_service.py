"""MemoryService — desktop-friendly wrapper over MemoryStore.

Provides memory RPC methods (status, search, update) consumed by the
desktop UI without modifying MemoryStore's internal structure.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from miqi.agent.memory import MemoryStore


class MemoryService:
    """Wraps :class:`MemoryStore` with desktop-oriented methods.

    All methods return plain dicts suitable for JSON-RPC responses.
    The underlying ``MemoryStore`` is not modified — the agent loop
    continues to use it directly.
    """

    def __init__(self, memory_store: MemoryStore) -> None:
        self._ms = memory_store

    # ── Status ──────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return memory store status."""
        raw = self._ms.get_status()
        # Redact file paths for safety in IPC transit
        safe = dict(raw)
        for key in ("snapshot_path", "audit_path", "lessons_file", "lessons_audit_file"):
            if key in safe:
                safe[key] = "..."
        return safe

    # ── Search ──────────────────────────────────────────────────────────

    def search(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        """Search across all memory stores: snapshot, lessons, daily notes.

        Returns matching items with their source and relevance.
        """
        if not query or not query.strip():
            return {"results": [], "count": 0, "query": query}

        q_lower = query.strip().lower()
        results: list[dict[str, Any]] = []

        # Search snapshot items
        for item in self._ms.list_snapshot_items(limit=200):
            text = item.get("text", "").lower()
            if q_lower in text or q_lower in item.get("session_key", "").lower():
                results.append({
                    "source": "snapshot",
                    "id": item.get("id", ""),
                    "text": item.get("text", "")[:200],
                    "hits": item.get("hits", 0),
                    "updated_at": item.get("updated_at", ""),
                })

        # Search lessons
        for lesson in self._ms.list_lessons(include_disabled=True, limit=200):
            trigger = lesson.get("trigger", "").lower()
            better = lesson.get("better_action", "").lower()
            if q_lower in trigger or q_lower in better:
                results.append({
                    "source": "lesson",
                    "id": lesson.get("id", ""),
                    "trigger": lesson.get("trigger", "")[:200],
                    "better_action": lesson.get("better_action", "")[:200],
                    "confidence": lesson.get("confidence", 0),
                    "enabled": lesson.get("enabled", True),
                })

        # Search daily notes
        results.extend(self._search_daily_notes(q_lower))

        # Cap results
        results = results[:limit]
        return {"results": results, "count": len(results), "query": query.strip()}

    # ── Update ──────────────────────────────────────────────────────────

    def update(self, text: str, action: str = "remember", **kwargs: Any) -> dict[str, Any]:
        """Unified update entry point — dispatches by action."""
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        if action == "remember":
            return self.remember(text, **kwargs)
        elif action == "append_today":
            return self.append_today(text)
        elif action == "learn_lesson":
            return self.learn_lesson(trigger=text, better_action=kwargs.get("better_action", text), **{k: v for k, v in kwargs.items() if k != "better_action"})
        else:
            raise ValueError(f"action must be 'remember', 'append_today', or 'learn_lesson', got '{action}'")

    def remember(self, text: str, session_key: str = "desktop:default", source: str = "desktop") -> dict[str, Any]:
        """Add or update a long-term memory item."""
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        self._ms.remember(text.strip(), session_key=session_key, source=source)
        self._ms.flush_if_needed()
        return {"action": "remember", "text_length": len(text.strip())}

    def append_today(self, content: str) -> dict[str, Any]:
        """Append content to today's daily notes."""
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        self._ms.append_today(content.strip())
        self._ms.flush_if_needed()
        return {"action": "append_today", "date": date.today().isoformat()}

    def learn_lesson(self, trigger: str, better_action: str, bad_action: str = "", session_key: str = "desktop:default", source: str = "desktop") -> dict[str, Any]:
        """Add or update a lesson."""
        if not trigger or not trigger.strip():
            raise ValueError("trigger must not be empty")
        if not better_action or not better_action.strip():
            raise ValueError("better_action must not be empty")
        self._ms.learn_lesson(
            trigger.strip(),
            bad_action.strip() if bad_action else "",
            better_action.strip(),
            session_key=session_key,
            source=source,
        )
        self._ms.flush_if_needed()
        return {"action": "learn_lesson", "trigger_length": len(trigger.strip())}

    # ── Delete ──────────────────────────────────────────────────────────

    def delete_snapshot_item(self, item_id: str) -> dict[str, Any]:
        """Delete a snapshot item by ID."""
        self._ms.delete_snapshot_item(item_id)
        self._ms.flush_if_needed()
        return {"action": "delete_snapshot_item", "item_id": item_id}

    def delete_lesson(self, lesson_id: str) -> dict[str, Any]:
        """Delete a lesson by ID."""
        self._ms.delete_lesson(lesson_id)
        self._ms.flush_if_needed()
        return {"action": "delete_lesson", "lesson_id": lesson_id}

    def set_lesson_enabled(self, lesson_id: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable a lesson."""
        self._ms.set_lesson_enabled(lesson_id, enabled)
        self._ms.flush_if_needed()
        return {"action": "set_lesson_enabled", "lesson_id": lesson_id, "enabled": enabled}

    # ── List ────────────────────────────────────────────────────────────

    def list_snapshot_items(self, session_key: str | None = None, limit: int = 50) -> dict[str, Any]:
        """List snapshot items."""
        items = self._ms.list_snapshot_items(session_key=session_key, limit=limit)
        return {"items": items, "count": len(items)}

    def list_lessons(self, include_disabled: bool = False, limit: int = 50) -> dict[str, Any]:
        """List lessons."""
        lessons = self._ms.list_lessons(include_disabled=include_disabled, limit=limit)
        return {"lessons": lessons, "count": len(lessons)}

    # ── Private helpers ─────────────────────────────────────────────────

    def _search_daily_notes(self, query_lower: str) -> list[dict[str, Any]]:
        """Search daily note files for the query."""
        results: list[dict[str, Any]] = []
        memory_dir = self._ms.memory_dir
        if not memory_dir.exists():
            return results

        try:
            for path in sorted(memory_dir.glob("????-??-??.md"), reverse=True):
                try:
                    content = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                if query_lower in content.lower():
                    date_str = path.stem
                    # Return a short excerpt around the match
                    idx = content.lower().find(query_lower)
                    start = max(0, idx - 50)
                    end = min(len(content), idx + 150)
                    excerpt = content[start:end].strip()
                    if start > 0:
                        excerpt = "..." + excerpt
                    if end < len(content):
                        excerpt = excerpt + "..."
                    results.append({
                        "source": "daily_note",
                        "date": date_str,
                        "excerpt": excerpt,
                    })
                    if len(results) >= 10:
                        break
        except Exception:
            pass
        return results
