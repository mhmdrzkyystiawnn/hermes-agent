"""Storage tests for the temporal provider — standalone, no pytest needed.

    python test_storage.py

Hermetic: creates its own temp HERMES_HOME and its own SessionDB; never touches
~/.hermes. Needs the repo on PYTHONPATH (for hermes_state) and the Hermes
interpreter (for ruamel etc.):

    HERMES_HOME=$(mktemp -d) PYTHONPATH=<repo> <hermes-venv>/bin/python3 test_storage.py
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
from storage import (  # noqa: E402
    ValidationError, active_source_ids, get_item, init_schema, insert_item,
    items_in_window, items_overlapping, mark_stale, schema_version,
)

TZ = ZoneInfo("Asia/Jakarta")
ANCHOR = datetime(2026, 10, 7, 10, 0, tzinfo=TZ).timestamp()

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"{'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")


def _base_item(**over):
    item = {
        "raw_expression": "besok",
        "summary": "makan sama Ina",
        "time_start": ANCHOR + 86400,
        "time_end": ANCHOR + 2 * 86400 - 1e-6,
        "precision": "hari",
        "resolved_tz": "Asia/Jakarta",
        "anchor_ts": ANCHOR,
        "source_session_id": "sess-1",
        "source_message_uid": "uid-aaa",
        "source_message_id": 11,
    }
    item.update(over)
    return item


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="temporal-storage-")
    os.environ["HERMES_HOME"] = tmp  # any accidental get_hermes_home() stays here
    db_path = os.path.join(tmp, "temporal.db")
    conn = sqlite3.connect(db_path)  # row_factory default = None, like plugin_db

    # 1. init idempotent + schema version in PRAGMA (v2 adds the pending queue)
    v1 = init_schema(conn)
    v2 = init_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("init idempotent (version 2 twice)", v1 == v2 == 2, f"{v1},{v2}")
    check("user_version readable", schema_version(conn) == 2)
    check("two tables: items + pending", tables == {"temporal_items", "temporal_pending"}, str(tables))
    check(
        "all items indexes exist",
        {"idx_temporal_time_start", "idx_temporal_window", "idx_temporal_src_msg", "idx_temporal_session"} <= idx,
        str(idx),
    )

    # 2. roundtrip
    iid = insert_item(conn, _base_item(ambiguous=True, alternatives=[(ANCHOR, ANCHOR + 100)]))
    got = get_item(conn, iid)
    check("roundtrip: raw_expression", got["raw_expression"] == "besok")
    check("roundtrip: summary", got["summary"] == "makan sama Ina")
    check("roundtrip: precision", got["precision"] == "hari")
    check("roundtrip: ambiguous bool", got["ambiguous"] is True)
    check("roundtrip: alternatives decoded", got["alternatives"] == [(ANCHOR, ANCHOR + 100)], str(got["alternatives"]))
    check("roundtrip: provenance", (got["source_session_id"], got["source_message_uid"], got["source_message_id"]) == ("sess-1", "uid-aaa", 11))
    check("roundtrip: defaults", (got["kind"], got["status"], got["epistemic"], got["confidence"]) == ("planned", "active", "factual", 1.0))
    check("roundtrip: not stale", got["stale"] is False)

    # 3. INCLUSIVE interval: [10:00, 11:00] — the 11:00 edge is INSIDE
    a = insert_item(conn, _base_item(raw_expression="slot", time_start=ANCHOR, time_end=ANCHOR + 3600, source_message_id=21))
    inside = [r["id"] for r in items_in_window(conn, ANCHOR + 3600, ANCHOR + 7200)]
    check("inclusive: item ending exactly at q.start is included", a in inside, str(inside))
    outside = [r["id"] for r in items_in_window(conn, ANCHOR + 3600 + 1e-3, ANCHOR + 7200)]
    check("exclusive side: item ending just before q.start is excluded", a not in outside, str(outside))
    touch_start = [r["id"] for r in items_in_window(conn, ANCHOR - 7200, ANCHOR)]
    check("inclusive: item starting exactly at q.end is included", a in touch_start, str(touch_start))

    # 4. window filters
    stale_id = insert_item(conn, _base_item(raw_expression="stale one", source_message_id=31))
    mark_stale(conn, source_message_ids=[31], reason="test")
    win = [r["id"] for r in items_in_window(conn, ANCHOR, ANCHOR + 3 * 86400)]
    check("window: stale item excluded by default", stale_id not in win, str(win))
    check("window: only_active=False sees it", stale_id in [r["id"] for r in items_in_window(conn, ANCHOR, ANCHOR + 3 * 86400, only_active=False)])
    far = [r["id"] for r in items_in_window(conn, ANCHOR + 10 * 86400, ANCHOR + 11 * 86400)]
    check("window: far item excluded", iid not in far, str(far))

    # 5. partial overlap for recall
    p = insert_item(conn, _base_item(raw_expression="partial", time_start=ANCHOR - 7200, time_end=ANCHOR + 1800, source_message_id=41))
    ov = [r["id"] for r in items_overlapping(conn, ANCHOR, ANCHOR + 3600)]
    check("overlap: partial intersection found", p in ov, str(ov))
    check("overlap: stale excluded by default", stale_id not in ov)
    check("overlap: include_stale sees it", stale_id in [r["id"] for r in items_overlapping(conn, ANCHOR, ANCHOR + 3 * 86400, include_stale=True)])

    # 6. null provenance is allowed
    n = insert_item(conn, _base_item(source_message_uid=None, source_message_id=None))
    check("nullable provenance ok", get_item(conn, n)["source_message_id"] is None)

    # 7. validation contract
    for label, bad, exc in (
        ("empty raw_expression", _base_item(raw_expression="  "), ValidationError),
        ("bad precision", _base_item(precision="dekade"), ValidationError),
        ("bad kind", _base_item(kind="maybe"), ValidationError),
        ("bad status", _base_item(status="zombie"), ValidationError),
        ("bad epistemic", _base_item(epistemic="dream"), ValidationError),
        ("confidence out of range", _base_item(confidence=1.5), ValidationError),
        ("end before start", _base_item(time_start=ANCHOR, time_end=ANCHOR - 1), ValidationError),
        ("non-numeric time_start", _base_item(time_start="besok"), ValidationError),
        ("missing session", _base_item(source_session_id=""), ValidationError),
    ):
        try:
            insert_item(conn, bad)
            check(f"reject: {label}", False, "no exception")
        except exc:
            check(f"reject: {label}", True)

    # 8. mark_stale scoping
    s2 = insert_item(conn, _base_item(raw_expression="other sess", source_session_id="sess-2", source_message_id=51))
    mark_stale(conn, source_message_ids=[51], reason="r", session_id="sess-1")
    check("mark_stale respects session filter", get_item(conn, s2)["stale"] is False)
    mark_stale(conn, source_message_ids=[], reason="r")
    check("mark_stale empty input is a no-op", get_item(conn, s2)["stale"] is False)

    conn.close()

    # 9. HERMES_HOME isolation
    check("db written under temp HERMES_HOME", os.path.exists(db_path))
    check("db path is inside the temp home", db_path.startswith(tmp), db_path)

    # 10. REWIND: prove MAX(id) is WRONG and membership is RIGHT.
    _rewind_proof(tmp)

    shutil.rmtree(tmp, ignore_errors=True)
    total = 0
    print(f"\n{'ALL PASSED' if _failures == 0 else str(_failures) + ' FAILED'}")
    return 1 if _failures else 0


def _rewind_proof(tmp: str) -> None:
    """Real SessionDB: show the compaction-handoff rewind breaks a MAX(id) boundary."""
    from pathlib import Path

    from hermes_state import SessionDB

    state_path = Path(tmp) / "state.db"
    db = SessionDB(db_path=state_path)
    sid = "rewind-sess"
    db.create_session(sid, "cli")
    ids = []
    for i in range(1, 5):
        ids.append(db.append_message(sid, "user", content=f"pesan {i}"))
        ids.append(db.append_message(sid, "assistant", content=f"balas {i}"))
    user_ids = ids[0::2]  # the 4 user rows
    check("rewind setup: 4 user rows", len(user_ids) == 4, str(user_ids))

    # Our storage rows pointing at each user message.
    tconn = sqlite3.connect(os.path.join(tmp, "rewind.db"))
    init_schema(tconn)
    for n, rid in enumerate(user_ids, start=1):
        insert_item(tconn, _base_item(
            raw_expression=f"hari {n}", source_session_id=sid, source_message_id=rid,
            time_start=ANCHOR + n * 86400, time_end=ANCHOR + (n + 1) * 86400 - 1e-6,
        ))

    # --- Case A: plain rewind to the 3rd user turn (no handoff scaffold) ---
    db.rewind_to_message(sid, user_ids[2])
    active = active_source_ids(state_path)
    check("case A: active ids readable", active is not None)
    stale_n = mark_stale(tconn, source_message_ids=[r for r in user_ids if r not in active], reason="rewound")
    check("case A: exactly the rewound items go stale", stale_n == 2, f"stale_n={stale_n}")
    check("case A: item 1 kept", get_item(tconn, 1)["stale"] is False)
    check("case A: item 3 stale", get_item(tconn, 3)["stale"] is True)
    check("case A: item 4 stale", get_item(tconn, 4)["stale"] is True)

    # --- Case B: rewind WITH compaction handoff -> a NEW row gets a HIGHER id ---
    from agent.context_compressor import (
        HISTORICAL_TASK_HEADING, SUMMARY_PREFIX, _SUMMARY_END_MARKER,
    )

    def _carrier(ask: str = "REAL ASK") -> str:
        return (
            f"{SUMMARY_PREFIX}\n{HISTORICAL_TASK_HEADING}\nold task\n\n"
            f"{_SUMMARY_END_MARKER}\n\n{ask}"
        )

    db2 = SessionDB(db_path=state_path)
    sid2 = "rewind-handoff"
    db2.create_session(sid2, "cli")
    db2.append_message(sid2, "user", content="tanya 1")
    db2.append_message(sid2, "assistant", content="jawab 1")
    carrier_id = db2.append_message(sid2, "user", content=_carrier())
    db2.append_message(sid2, "assistant", content="jawab 2")
    user_ids2 = [r["_row_id"] for r in db2.get_messages_as_conversation(sid2, include_row_ids=True) if r["role"] == "user"]
    check("case B setup: 2 user rows (plain + carrier)", len(user_ids2) == 2, str(user_ids2))

    t2 = sqlite3.connect(os.path.join(tmp, "rewind2.db"))
    init_schema(t2)
    for n, rid in enumerate(user_ids2, start=1):
        insert_item(t2, _base_item(
            raw_expression=f"tanya {n}", source_session_id=sid2, source_message_id=rid,
            time_start=ANCHOR + n * 86400, time_end=ANCHOR + (n + 1) * 86400 - 1e-6,
        ))

    before = active_source_ids(state_path)
    max_before = max(before)
    result = db2.rewind_to_message(
        sid2, carrier_id,
        preserve_compaction_handoff=True,
        expected_target_content="REAL ASK",
    )
    active2 = active_source_ids(state_path)
    max_after = max(active2)
    replacement_id = result.get("replacement_message_id")
    check("case B: handoff inserted a replacement row", replacement_id is not None, str(result))
    check(
        "case B: replacement row got a HIGHER id than the rewind target",
        replacement_id is not None and replacement_id > carrier_id,
        f"replacement={replacement_id} target={carrier_id}",
    )
    check(
        "case B: a MAX(id) boundary would be WRONG (it sits past the target)",
        max_after >= replacement_id,
        f"max_after={max_after} replacement={replacement_id}",
    )
    # Membership rule stays correct: the carrier row is inactive, the earlier row is not.
    check("case B: carrier row is no longer active", carrier_id not in active2)
    check("case B: first user row still active", user_ids2[0] in active2)

    stale_n2 = mark_stale(t2, source_message_ids=[r for r in user_ids2 if r not in active2], reason="rewound")
    check("case B: membership marks exactly the rewound item", stale_n2 == 1, f"stale_n={stale_n2}")
    check("case B: item 1 (plain) kept", get_item(t2, 1)["stale"] is False)
    check("case B: item 2 (carrier) stale", get_item(t2, 2)["stale"] is True)

    db.close()
    db2.close()
    tconn.close()
    t2.close()


if __name__ == "__main__":
    sys.exit(main())
