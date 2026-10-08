"""Temporal memory provider (MemoryProvider).

Steps 1-3 done: skeleton + activation, date resolver, storage, extraction.
Injection (prefetch) and the recall tool are NOT implemented yet — see
TEMPORAL_MEMORY_PLAN.md Fase 1.

Design notes carried from Fase 0:
  * `initialize()` receives `agent_context` ("primary" | "subagent" | "cron" |
    "flush"); the guard for cron/subagent lives HERE.
  * `hermes_home` arrives in initialize() kwargs — never hardcode ~/.hermes.
  * `system_prompt_block()` stays empty: the system prompt must be byte-stable
    for the life of a conversation (prompt-caching invariant).
  * `prefetch()` must be fast and return cached content.
  * Rewind detection uses row MEMBERSHIP in state.db, never a MAX(id) boundary
    (see storage.py docstring for the compaction-handoff counterexample).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

import extract
import recall
import resolver
import storage

logger = logging.getLogger(__name__)

# Agent contexts whose turns must NOT be recorded (Fase 0 / H3: hooks fire there too).
SKIP_CONTEXTS = frozenset({"cron", "subagent", "flush"})

_lock = threading.Lock()
# Drain serialization per home (D2/D4): on_session_end can run on the manager's
# worker thread (/new) while on_turn_start runs on the main thread. One lock per
# home so a slow drain in one profile never blocks another.
_drain_locks: Dict[str, threading.Lock] = {}
_drain_locks_guard = threading.Lock()


def _drain_lock(hermes_home: str) -> threading.Lock:
    with _drain_locks_guard:
        lock = _drain_locks.get(hermes_home)
        if lock is None:
            lock = threading.Lock()
            _drain_locks[hermes_home] = lock
        return lock

TZ_NAME = "Asia/Jakarta"


def _data_dir(hermes_home: str) -> Path:
    """`<home>/plugin-data/temporal/` — the plugin-data convention, not the install dir."""
    root = Path(hermes_home) / "plugin-data" / "temporal"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _append_event(hermes_home: str, event: dict) -> None:
    """Append one JSON line to the lifecycle log. Best-effort, never raises into the host."""
    if not hermes_home:
        return
    try:
        with _lock:
            with (_data_dir(hermes_home) / "events.log").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("temporal: could not write lifecycle event: %s", exc)


def _resolve_tz(tz_name: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone
        return timezone.utc


class TemporalMemoryProvider(MemoryProvider):
    """Stores time-bound statements with absolute dates; recall lands later."""

    def __init__(self) -> None:
        self._home: str = ""
        self._session_id: str = ""
        self._agent_context: str = ""
        self._active: bool = False
        self._conn: Optional[sqlite3.Connection] = None
        self._tz = _resolve_tz(TZ_NAME)
        self._last_recall_count: int = 0

    # -- identity -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "temporal"

    def is_available(self) -> bool:
        """No credentials, no network needed. Checked before `initialize()`."""
        return True

    # -- lifecycle ------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Called once at agent startup. Binds the profile home + opens our DB."""
        self._home = str(kwargs.get("hermes_home") or "")
        self._session_id = session_id
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._active = self._agent_context not in SKIP_CONTEXTS

        self._tz = _resolve_tz(str(kwargs.get("timezone") or TZ_NAME))

        opened = False
        try:
            self._conn = storage.open_db_for_home(self._home)
            storage.init_schema(self._conn)
            opened = True
        except Exception as exc:
            logger.warning("temporal: could not open storage (fail-open, provider inert): %s", exc)
            self._conn = None

        _append_event(self._home, {
            "event": "initialize",
            "session_id": session_id,
            "agent_context": self._agent_context,
            "platform": kwargs.get("platform"),
            "active": self._active,
            "storage_open": opened,
        })

    def shutdown(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        _append_event(self._home, {"event": "shutdown", "session_id": self._session_id})

    # -- per-turn -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        """Empty on purpose: byte-stable system prompt (prompt-caching invariant)."""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Near-term temporal recall for THIS turn (Fase 1 langkah 4).

        Returns plaintext; core fences it in `<memory-context>`. The block is capped
        (MAX_ITEMS/MAX_CHARS) — that bounds the NEW bytes per turn, but core replays
        each turn's sidecar, so total replay stays O(n^2). Accepted (D1).
        """
        if not self._active or self._conn is None:
            return ""
        try:
            block = recall.build_context(self._conn, query=query or "", tz=self._tz)
        except Exception as exc:  # fail-open (D11)
            logger.debug("temporal: prefetch failed (fail-open): %s", exc)
            self._last_recall_count = 0
            return ""
        self._last_recall_count = sum(1 for ln in block.splitlines() if ln.startswith("- ")) if block else 0
        if self._last_recall_count:
            _append_event(self._home, {
                "event": "prefetch",
                "session_id": self._session_id,
                "items": self._last_recall_count,
                "chars": len(block),
                "historical": recall.is_historical_request(query or ""),
            })
        return block

    def recall_status(self):
        """Reflect the LAST prefetch (core renders the 🧠 indicator)."""
        try:
            from agent.memory_provider import RecallStatus
        except Exception:
            return None
        if not self._last_recall_count:
            return None
        return RecallStatus(provider_label="Temporal", count=self._last_recall_count)

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: List[Dict[str, Any]] | None = None,
        turn_author: Dict[str, Any] | None = None,
    ) -> None:
        """Gate + observability ONLY — no LLM here.

        A2 (approved): extraction moved to `on_session_end`, which runs synchronously
        outside the 5s `_SYNC_DRAIN_TIMEOUT_S` budget that was silently dropping
        per-turn writes. Calling the LLM here is what lost turns.
        """
        if not self._active:
            return
        _append_event(self._home, {
            "event": "sync_turn",
            "session_id": session_id,
            "user_len": len(user_content or ""),
            "gated": extract.gate(user_content or ""),
        })

    # -- session boundaries ---------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Enqueue the session's gated messages, then drain one batch (D4).

        Runs SYNCHRONOUSLY and before MemoryManager.shutdown_all()'s bounded drain,
        so it is not subject to the 5s budget. Enqueue commits BEFORE the LLM call,
        so anything past the batch stays durable in temporal_pending for a later
        on_turn_start. Idempotent via (session, row id).
        """
        result: Dict[str, Any] = {"candidates": 0, "enqueued": 0, "skipped_done": 0,
                                  "drained": 0, "items": 0, "failed": 0, "remaining": 0}
        t0 = time.time()
        if self._active and self._conn is not None and self._session_id:
            try:
                with _drain_lock(self._home):
                    result = extract.process_session(
                        self._conn, messages=messages or [], session_id=self._session_id,
                        tz=self._tz,
                    )
            except Exception as exc:  # extract is fail-open; belt and braces
                logger.debug("temporal: on_session_end failed (fail-open): %s", exc)
        _append_event(self._home, {
            "event": "on_session_end",
            "session_id": self._session_id,
            "messages": len(messages or []),
            "duration_s": round(time.time() - t0, 2),
            **result,
        })

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Drain one pending batch per turn (D2) — the continuation of a backlog.

        Runs off the exit path, so no watchdog applies here. No-op when nothing is
        pending (the common case: a small session drains fully in on_session_end).
        """
        if not self._active or self._conn is None:
            return
        t0 = time.time()
        result = {"drained": 0, "items": 0, "failed": 0, "remaining": 0}
        try:
            if storage.pending_count(self._conn) > 0:
                with _drain_lock(self._home):
                    result = extract.drain_batch(self._conn, tz=self._tz)
        except Exception as exc:  # fail-open
            logger.debug("temporal: on_turn_start drain failed (fail-open): %s", exc)
        if result["drained"] or result["failed"]:
            _append_event(self._home, {
                "event": "on_turn_start_drain",
                "session_id": self._session_id,
                "turn_number": turn_number,
                "duration_s": round(time.time() - t0, 2),
                **result,
            })

    def on_session_switch(
        self, new_session_id: str, *, parent_session_id: str = "",
        reset: bool = False, rewound: bool = False, **kwargs,
    ) -> None:
        """session_id reassigned without teardown. `rewound=True` on /undo."""
        if rewound and self._conn is not None and self._home:
            self._reconcile_rewind(self._session_id)
        _append_event(self._home, {
            "event": "on_session_switch",
            "new_session_id": new_session_id,
            "parent_session_id": parent_session_id,
            "reset": reset,
            "rewound": rewound,
        })
        self._session_id = new_session_id

    def _reconcile_rewind(self, session_id: str) -> None:
        """Mark items whose source row is no longer active.

        Membership, not a MAX(id) boundary — see storage.py. Reading state.db is
        read-only; a failure skips rather than guessing.
        """
        if not session_id or self._conn is None:
            return
        try:
            active = storage.active_source_ids(Path(self._home) / "state.db")
            if active is None:
                return
            rows = self._conn.execute(
                "SELECT DISTINCT source_message_id FROM temporal_items "
                "WHERE source_session_id = ? AND stale = 0 AND source_message_id IS NOT NULL",
                (session_id,),
            ).fetchall()
            gone = [int(r[0]) for r in rows if int(r[0]) not in active]
            n = storage.mark_stale(self._conn, source_message_ids=gone,
                                   reason="rewound", session_id=session_id) if gone else 0
            _append_event(self._home, {"event": "rewind_reconcile", "session_id": session_id,
                                       "candidates": len(rows), "marked_stale": n})
        except Exception as exc:
            logger.debug("temporal: rewind reconcile failed (fail-open): %s", exc)

    # -- tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """`temporal_recall` — deterministic date-range recall over stored items."""
        return [{
            "name": "temporal_recall",
            "description": (
                "Look up time-bound things the user told you earlier (plans, events) by a "
                "date or time expression in Indonesian, e.g. \"tanggal 12 september\", "
                "\"bulan lalu\", \"dua hari lalu\", \"minggu depan\". Resolves the expression "
                "to an absolute range and returns matching stored items with their dates. "
                "Use when the user asks what happened / what is planned around a time. "
                "Near-term items are already injected automatically; call this for an "
                "explicit time the user names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The time expression to look up, copied from the user (e.g. 'tanggal 12 september', 'bulan lalu').",
                    },
                    "include_cancelled": {
                        "type": "boolean",
                        "description": "Include cancelled/superseded items (default false).",
                    },
                },
                "required": ["expression"],
            },
        }]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "temporal_recall":
            return tool_error(f"Unknown temporal tool: {tool_name}")
        if not self._active or self._conn is None:
            return tool_error("temporal memory is not available in this session")
        try:
            expression = str((args or {}).get("expression") or "").strip()
            if not expression:
                return tool_error("'expression' is required")
            include_cancelled = bool((args or {}).get("include_cancelled"))
            res = recall.recall_range(
                self._conn, expression=expression, tz=self._tz,
                include_cancelled=include_cancelled,
            )
            if not res["matched"]:
                return json.dumps({"matched": False, "expression": expression,
                                   "items": [], "note": "could not resolve the time expression"})
            return json.dumps({
                "matched": True,
                "expression": expression,
                "resolved_range": {"start": res["start"], "end": res["end"],
                                   "precision": res["precision"]},
                "items": res["items"],
                "count": len(res["items"]),
            }, ensure_ascii=False, default=str)
        except Exception as exc:  # fail-open
            logger.debug("temporal: temporal_recall failed (fail-open): %s", exc)
            return tool_error(f"temporal recall failed: {exc}")


def register(ctx) -> None:
    """Discovery entry point: register the provider and its auxiliary task slot."""
    try:
        ctx.register_auxiliary_task(
            extract.AUX_TASK_KEY,
            display_name="Temporal extraction",
            description="date extraction for temporal memory",
        )
    except Exception as exc:
        logger.debug("temporal: auxiliary task registration skipped: %s", exc)
    ctx.register_memory_provider(TemporalMemoryProvider())
