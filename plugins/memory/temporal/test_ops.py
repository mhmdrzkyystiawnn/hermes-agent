"""Operational CRUD tests (Fase 1 langkah 6): storage list/delete + CLI rendering.

    HERMES_HOME=<tmp> PYTHONPATH=<repo> <hermes-venv>/bin/python3 test_ops.py

Covers: empty list, multiple items, range/status/stale filters, delete existing,
delete non-existing, stale identifiable, cancelled visible as a status, and that
delete NEVER touches temporal_pending.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage  # noqa: E402

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=TZ).timestamp()

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"{'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")


def _day(offset: float, hour: int = 10) -> float:
    return (datetime(2026, 10, 7, hour, 0, tzinfo=TZ) + __import__("datetime").timedelta(days=offset)).timestamp()


def _item(conn, *, start, end, summary, status="active", stale=False, mid=None):
    return storage.insert_item(conn, {
        "raw_expression": summary, "summary": summary, "time_start": start, "time_end": end,
        "precision": "hari", "kind": "planned", "status": status, "epistemic": "factual",
        "confidence": 1.0, "anchor_ts": NOW, "source_session_id": "s", "source_message_id": mid,
        "stale": stale, "stale_reason": "rewound" if stale else None,
    })


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="temporal-ops-")
    os.environ["HERMES_HOME"] = tmp
    conn = sqlite3.connect(os.path.join(tmp, "o.db"))
    storage.init_schema(conn)

    # --- empty list ---
    check("empty: list returns []", storage.list_items(conn) == [])
    check("empty: count_items 0", storage.count_items(conn) == 0)

    # --- multiple items ---
    id1 = _item(conn, start=_day(1), end=_day(1, 23), summary="makan sama ina", mid=1)
    id2 = _item(conn, start=_day(-2), end=_day(-2, 23), summary="kecelakaan", mid=2)
    id3 = _item(conn, start=_day(5), end=_day(5, 23), summary="ke bogor", mid=3)
    rows = storage.list_items(conn)
    check("multi: all 3 listed", len(rows) == 3, str(len(rows)))
    check("multi: ordered by time_start", [r["summary"] for r in rows] == ["kecelakaan", "makan sama ina", "ke bogor"],
          str([r["summary"] for r in rows]))

    # --- range filter ---
    s, e = _day(-3), _day(2, 23)
    rng = storage.list_items(conn, start=s, end=e)
    check("range: only items overlapping", {r["summary"] for r in rng} == {"kecelakaan", "makan sama ina"},
          str([r["summary"] for r in rng]))

    # --- status filter ---
    id4 = _item(conn, start=_day(3), end=_day(3, 23), summary="acara batal", status="cancelled", mid=4)
    cancelled = storage.list_items(conn, status="cancelled")
    check("status: cancelled filter works", [r["summary"] for r in cancelled] == ["acara batal"],
          str([r["summary"] for r in cancelled]))
    check("status: cancelled visible as its own status",
          storage.list_items(conn, status="cancelled")[0]["status"] == "cancelled")

    # --- stale: shown by default, identifiable, hideable ---
    id5 = _item(conn, start=_day(4), end=_day(4, 23), summary="item stale", stale=True, mid=5)
    allrows = storage.list_items(conn)
    check("stale: shown by default", any(r["id"] == id5 for r in allrows), str([r["id"] for r in allrows]))
    stale_row = [r for r in allrows if r["id"] == id5][0]
    check("stale: identifiable via flag + reason", stale_row["stale"] is True and stale_row["stale_reason"] == "rewound")
    no_stale = storage.list_items(conn, include_stale=False)
    check("stale: hideable with include_stale=False", all(not r["stale"] for r in no_stale),
          str([(r["id"], r["stale"]) for r in no_stale]))

    # --- limit ---
    check("limit: caps rows", len(storage.list_items(conn, limit=2)) == 2)

    # --- delete existing ---
    before = storage.count_items(conn)
    ok = storage.delete_item(conn, id2)
    check("delete: existing returns True", ok is True)
    check("delete: row count drops by 1", storage.count_items(conn) == before - 1,
          f"{before} -> {storage.count_items(conn)}")
    check("delete: only that id removed", id2 not in {r["id"] for r in storage.list_items(conn)})

    # --- delete non-existing ---
    check("delete: non-existing returns False", storage.delete_item(conn, 999999) is False)
    check("delete: count unchanged after miss", storage.count_items(conn) == before - 1)

    # --- delete is id-based, never heuristic: neighbouring ids survive ---
    check("delete: other ids untouched", {id1, id3, id4, id5} <= {r["id"] for r in storage.list_items(conn)})

    # --- delete NEVER touches temporal_pending ---
    storage.enqueue_pending(conn, [{
        "source_session_id": "s", "source_message_id": 500, "anchor_ts": NOW, "content": "besok gue pergi",
    }])
    pend_before = storage.pending_count(conn)
    storage.delete_item(conn, id1)
    check("delete: temporal_pending untouched", storage.pending_count(conn) == pend_before,
          f"{pend_before} -> {storage.pending_count(conn)}")

    # --- delete rejects a non-integer id ---
    try:
        storage.delete_item(conn, "abc")
        check("delete: non-integer id rejected", False, "no raise")
    except storage.ValidationError:
        check("delete: non-integer id rejected", True)

    # --- CLI rendering (import the cli module directly) ---
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cli as temporal_cli  # noqa: E402
    row = storage.get_item(conn, id5)
    line = temporal_cli._render_row(row, TZ)
    check("cli render: shows id", f"[{id5}]" in line, line)
    check("cli render: shows STALE tag", "[STALE:rewound]" in line, line)
    row4 = storage.get_item(conn, id4)
    check("cli render: cancelled visible", "cancelled" in temporal_cli._render_row(row4, TZ))

    # --- CLI argparse wiring ---
    import argparse  # noqa: E402
    parser = argparse.ArgumentParser(prog="hermes temporal")
    temporal_cli.register_cli(parser)
    ns = parser.parse_args(["list", "--status", "cancelled", "--no-stale", "--limit", "5"])
    check("cli argparse: list flags parsed",
          ns.status == "cancelled" and ns.no_stale is True and ns.limit == 5 and ns.func is temporal_cli._cmd_list)
    ns2 = parser.parse_args(["delete", "42"])
    check("cli argparse: delete id parsed", ns2.id == "42" and ns2.func is temporal_cli._cmd_delete)

    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASSED' if _failures == 0 else str(_failures) + ' FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
