"""Storage layer for the temporal memory provider.

Two tables (schema version in `PRAGMA user_version`):

* ``temporal_items`` — resolved time-bound items (the recall surface).
* ``temporal_pending`` — durable work queue (D1, approved). A gate-passing user
  message is enqueued BEFORE the LLM runs, and deleted only after extraction
  succeeds; a crash/hard-kill therefore leaves work to be drained on a later
  lifecycle instead of losing it. Dedup identity is
  ``(source_session_id, source_message_id)`` — the same identity as items.

See TEMPORAL_MEMORY_PLAN.md Fase 1 langkah 2/3.

Interval convention: INCLUSIVE on both ends, `[start, end]`, epoch seconds UTC.
The resolver already produces inclusive ends (`+1 day - 1µs`), so no conversion
happens here. Overlap is therefore: `item.start <= q.end AND item.end >= q.start`.

Provenance: `source_message_id` is the durable `_row_id` of the row the item came
from. Rewind detection uses MEMBERSHIP in the active row set, never a MAX(id)
boundary — `rewind_to_message` can insert a replacement row with a HIGHER id when
it preserves a compaction handoff, which would make a MAX(id) boundary silently
wrong.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

PLUGIN_DATA_NAME = "temporal"
DB_FILENAME = "data.db"

# Allowed values (contract, not decoration). Unknown values are rejected on insert.
PRECISIONS = frozenset({"hari", "bagian hari", "minggu", "bulan", "tahun"})
KINDS = frozenset({"planned", "occurred", "ongoing"})
STATUSES = frozenset({"active", "cancelled", "superseded"})
EPISTEMICS = frozenset({"factual", "hypothetical", "third_party", "negated"})

_DDL = """
CREATE TABLE IF NOT EXISTS temporal_items (
  id                 INTEGER PRIMARY KEY,
  raw_expression     TEXT    NOT NULL,
  summary            TEXT    NOT NULL DEFAULT '',
  time_start         REAL    NOT NULL,
  time_end           REAL    NOT NULL,
  precision          TEXT    NOT NULL,
  ambiguous          INTEGER NOT NULL DEFAULT 0,
  alternatives       TEXT,
  resolved_tz        TEXT    NOT NULL DEFAULT '',
  kind               TEXT    NOT NULL DEFAULT 'planned',
  status             TEXT    NOT NULL DEFAULT 'active',
  epistemic          TEXT    NOT NULL DEFAULT 'factual',
  confidence         REAL    NOT NULL DEFAULT 1.0,
  anchor_ts          REAL    NOT NULL,
  source_session_id  TEXT    NOT NULL,
  source_message_uid TEXT,
  source_message_id  INTEGER,
  created_at         REAL    NOT NULL,
  updated_at         REAL,
  stale              INTEGER NOT NULL DEFAULT 0,
  stale_reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_temporal_time_start ON temporal_items(time_start);
CREATE INDEX IF NOT EXISTS idx_temporal_window
  ON temporal_items(status, stale, time_start, time_end);
CREATE INDEX IF NOT EXISTS idx_temporal_src_msg ON temporal_items(source_message_id);
CREATE INDEX IF NOT EXISTS idx_temporal_session ON temporal_items(source_session_id);
CREATE TABLE IF NOT EXISTS temporal_pending (
  id                 INTEGER PRIMARY KEY,
  source_session_id  TEXT    NOT NULL,
  source_message_id  INTEGER,
  source_message_uid TEXT,
  anchor_ts          REAL    NOT NULL,
  content            TEXT    NOT NULL,
  created_at         REAL    NOT NULL,
  UNIQUE(source_session_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_temporal_pending_order ON temporal_pending(id);
"""

_COLUMNS = (
    "id", "raw_expression", "summary", "time_start", "time_end", "precision",
    "ambiguous", "alternatives", "resolved_tz", "kind", "status", "epistemic",
    "confidence", "anchor_ts", "source_session_id", "source_message_uid",
    "source_message_id", "created_at", "updated_at", "stale", "stale_reason",
)

_INSERT_COLUMNS = tuple(c for c in _COLUMNS if c != "id")


class ValidationError(ValueError):
    """A field carries a value outside its declared contract."""


def init_schema(conn: sqlite3.Connection) -> int:
    """Create the table + indexes; set `PRAGMA user_version`. Idempotent.

    Returns the schema version now in effect.
    """
    conn.executescript(_DDL)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < SCHEMA_VERSION:
        # PRAGMA can't be parameterised; SCHEMA_VERSION is our own int literal.
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        current = SCHEMA_VERSION
    conn.commit()
    return current


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def db_path_for_home(hermes_home) -> "Path":
    """`<home>/plugin-data/temporal/data.db` — the plugin-data convention."""
    from pathlib import Path

    return Path(hermes_home) / "plugin-data" / PLUGIN_DATA_NAME / DB_FILENAME


def open_db_for_home(hermes_home) -> sqlite3.Connection:
    """Open OUR db at an EXPLICIT home.

    Deliberately not `plugins.plugin_storage.plugin_db()`: that resolves
    `get_hermes_home()` (a process-global) on every call, which is the wrong home
    when one process serves several profiles. `initialize()` hands us the profile's
    own `hermes_home`, so we bind to that. Same PRAGMAs (WAL + fallback, busy
    timeout, row_factory=None) through the shared opener.
    """
    from hermes_cli.sqlite_util import open_db

    path = db_path_for_home(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open_db(path, db_label=f"plugin-data/{PLUGIN_DATA_NAME}/{DB_FILENAME}",
                   foreign_keys=True, row_factory=None, check_same_thread=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(item: Dict[str, Any]) -> None:
    if not isinstance(item.get("raw_expression"), str) or not item["raw_expression"].strip():
        raise ValidationError("raw_expression must be a non-empty string")
    for field_name in ("time_start", "time_end", "anchor_ts"):
        value = item.get(field_name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValidationError(f"{field_name} must be a number (epoch seconds)")
    if item["time_end"] < item["time_start"]:
        raise ValidationError("time_end must be >= time_start (inclusive interval)")
    if item.get("precision") not in PRECISIONS:
        raise ValidationError(f"precision {item.get('precision')!r} not in {sorted(PRECISIONS)}")
    for field_name, allowed in (("kind", KINDS), ("status", STATUSES), ("epistemic", EPISTEMICS)):
        value = item.get(field_name, _DEFAULTS[field_name])
        if value not in allowed:
            raise ValidationError(f"{field_name} {value!r} not in {sorted(allowed)}")
    conf = item.get("confidence", 1.0)
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0.0 <= float(conf) <= 1.0):
        raise ValidationError("confidence must be a number in [0.0, 1.0]")
    if not isinstance(item.get("source_session_id"), str) or not item["source_session_id"]:
        raise ValidationError("source_session_id must be a non-empty string")


_DEFAULTS = {"kind": "planned", "status": "active", "epistemic": "factual"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def insert_item(conn: sqlite3.Connection, item: Dict[str, Any]) -> int:
    """Insert one item; returns the new row id. Raises ValidationError on bad input."""
    _validate(item)
    now = time.time()
    row = {c: None for c in _INSERT_COLUMNS}
    row.update(item)

    def _val(col, default=None):
        """Default only when absent OR None — `row` pre-seeds every column with None."""
        v = row.get(col)
        return default if v is None else v

    row["raw_expression"] = str(row["raw_expression"])
    row["summary"] = str(_val("summary", ""))
    row["ambiguous"] = 1 if _val("ambiguous", False) else 0
    row["alternatives"] = _encode_alternatives(_val("alternatives"))
    row["resolved_tz"] = str(_val("resolved_tz", ""))
    row["kind"] = _val("kind", "planned")
    row["status"] = _val("status", "active")
    row["epistemic"] = _val("epistemic", "factual")
    row["confidence"] = float(_val("confidence", 1.0))
    row["source_message_uid"] = _val("source_message_uid")
    row["source_message_id"] = _val("source_message_id")
    row["created_at"] = float(_val("created_at", now))
    row["updated_at"] = _val("updated_at")
    row["stale"] = 1 if _val("stale", False) else 0
    row["stale_reason"] = _val("stale_reason")

    placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
    cur = conn.execute(
        f"INSERT INTO temporal_items ({', '.join(_INSERT_COLUMNS)}) VALUES ({placeholders})",
        tuple(row[c] for c in _INSERT_COLUMNS),
    )
    conn.commit()
    return int(cur.lastrowid)


def _encode_alternatives(value: Any) -> Optional[str]:
    """Accept [[start, end], ...] (tuples or lists) or None; store as JSON text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value  # already JSON
    pairs = [[float(s), float(e)] for s, e in value]
    return json.dumps(pairs)


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    """`plugin_db` opens with row_factory=None, so rows arrive as tuples."""
    item = dict(zip(_COLUMNS, row))
    item["ambiguous"] = bool(item["ambiguous"])
    item["stale"] = bool(item["stale"])
    if item["alternatives"]:
        item["alternatives"] = [tuple(p) for p in json.loads(item["alternatives"])]
    return item


def get_item(conn: sqlite3.Connection, item_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM temporal_items WHERE id = ?", (int(item_id),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def items_in_window(
    conn: sqlite3.Connection, start: float, end: float, *, only_active: bool = True,
) -> List[Dict[str, Any]]:
    """Items whose [time_start, time_end] INTERSECTS [start, end] (inclusive)."""
    clauses = ["time_start <= ?", "time_end >= ?"]
    params: List[Any] = [float(end), float(start)]
    if only_active:
        clauses += ["status = 'active'", "stale = 0"]
    sql = (
        f"SELECT {', '.join(_COLUMNS)} FROM temporal_items WHERE {' AND '.join(clauses)} "
        "ORDER BY time_start, id"
    )
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def items_overlapping(
    conn: sqlite3.Connection, start: float, end: float, *, include_stale: bool = False,
) -> List[Dict[str, Any]]:
    """Like items_in_window but for recall: cancelled/superseded allowed, stale opt-in."""
    clauses = ["time_start <= ?", "time_end >= ?"]
    params: List[Any] = [float(end), float(start)]
    if not include_stale:
        clauses.append("stale = 0")
    sql = (
        f"SELECT {', '.join(_COLUMNS)} FROM temporal_items WHERE {' AND '.join(clauses)} "
        "ORDER BY time_start, id"
    )
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def mark_stale(
    conn: sqlite3.Connection, *, source_message_ids: Iterable[int],
    reason: str = "", session_id: Optional[str] = None,
) -> int:
    """Mark items stale by SOURCE ROW MEMBERSHIP. Returns the number updated.

    Membership, not a MAX(id) boundary: `rewind_to_message` can insert a
    replacement row with a higher id (compaction-handoff case), so any
    "everything above N" rule is wrong. See module docstring.
    """
    ids = [int(i) for i in source_message_ids]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    clauses = [f"source_message_id IN ({placeholders})", "stale = 0"]
    params: List[Any] = list(ids)
    if session_id is not None:
        clauses.append("source_session_id = ?")
        params.append(session_id)
    sql = (
        f"UPDATE temporal_items SET stale = 1, stale_reason = ?, updated_at = ? "
        f"WHERE {' AND '.join(clauses)}"
    )
    cur = conn.execute(sql, [reason, time.time(), *params])
    conn.commit()
    return int(cur.rowcount)


def active_source_ids(state_db_path) -> Optional[set]:
    """Active `messages.id` set for rewind reconciliation, or None when unreadable.

    Reads the CORE `state.db` (read-only) — the provider does not own it. A
    failure returns None so callers can skip rather than guess.
    """
    try:
        uri = f"file:{state_db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return {int(r[0]) for r in conn.execute("SELECT id FROM messages WHERE active = 1")}
        finally:
            conn.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Durable pending queue (D1)
# ---------------------------------------------------------------------------

_PENDING_COLUMNS = (
    "id", "source_session_id", "source_message_id", "source_message_uid",
    "anchor_ts", "content", "created_at",
)


def enqueue_pending(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    """Insert pending work rows, ignoring ones already queued. Returns rows added.

    ``INSERT OR IGNORE`` on the ``(source_session_id, source_message_id)`` unique
    key makes a repeated ``on_session_end`` a no-op (idempotent). Committed here,
    BEFORE any LLM call, so a later crash still leaves the work durable.

    NOTE: a message with no ``_row_id`` (test stubs / partial agents) carries a
    NULL ``source_message_id``; SQLite treats NULLs as distinct in a UNIQUE index,
    so such rows are NOT deduplicated. The real provider path always has a row id.
    """
    added = 0
    now = time.time()
    for row in rows:
        sid = row.get("source_session_id")
        if not isinstance(sid, str) or not sid:
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        mid = row.get("source_message_id")
        if not (isinstance(mid, int) and not isinstance(mid, bool)):
            mid = None
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO temporal_pending "
                "(source_session_id, source_message_id, source_message_uid, anchor_ts, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, mid, row.get("source_message_uid"),
                 float(row.get("anchor_ts") or now), content, float(row.get("created_at") or now)),
            )
            added += int(cur.rowcount or 0)
        except Exception as exc:  # fail-open: one bad row must not stop the rest
            logger.debug("temporal: enqueue_pending row skipped (fail-open): %s", exc)
    conn.commit()
    return added


def next_pending(conn: sqlite3.Connection, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Oldest-first (FIFO, D3) slice of the queue."""
    if not limit or limit <= 0:
        return []
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_PENDING_COLUMNS)} FROM temporal_pending "
            "ORDER BY id LIMIT ?", (int(limit),),
        ).fetchall()
    except Exception:
        return []
    return [dict(zip(_PENDING_COLUMNS, r)) for r in rows]


def delete_pending(conn: sqlite3.Connection, pending_ids: Iterable[int]) -> int:
    """Remove drained rows. Called ONLY after their extraction succeeded."""
    ids = [int(i) for i in pending_ids]
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    cur = conn.execute(f"DELETE FROM temporal_pending WHERE id IN ({placeholders})", ids)
    conn.commit()
    return int(cur.rowcount or 0)


def pending_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM temporal_pending").fetchone()[0])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Operational CRUD (Fase 1 langkah 6 — CLI list/delete)
# ---------------------------------------------------------------------------


def list_items(
    conn: sqlite3.Connection, *, start: Optional[float] = None, end: Optional[float] = None,
    status: Optional[str] = None, include_stale: bool = True, limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Deterministic listing ordered by (time_start, id).

    No filters -> every row. ``start``/``end`` window the interval overlap
    (inclusive, same convention as items_in_window). ``status`` filters exactly.
    ``include_stale`` defaults True so the CLI can SHOW stale rows (they are not
    silently treated as active). Never raises.
    """
    try:
        clauses: List[str] = []
        params: List[Any] = []
        if start is not None and end is not None:
            clauses += ["time_start <= ?", "time_end >= ?"]
            params += [float(end), float(start)]
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        if not include_stale:
            clauses.append("stale = 0")
        sql = f"SELECT {', '.join(_COLUMNS)} FROM temporal_items"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY time_start, id"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as exc:
        logger.debug("temporal: list_items failed (fail-open): %s", exc)
        return []


def delete_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Delete ONE item by primary key. Returns True when a row was removed.

    Explicit and deterministic: only the given id is touched. ``temporal_pending``
    is NEVER touched here. Raises ValidationError for a non-integer id.
    """
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise ValidationError("item_id must be an integer")
    cur = conn.execute("DELETE FROM temporal_items WHERE id = ?", (int(item_id),))
    conn.commit()
    return int(cur.rowcount or 0) > 0


def count_items(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM temporal_items").fetchone()[0])
    except Exception:
        return 0
