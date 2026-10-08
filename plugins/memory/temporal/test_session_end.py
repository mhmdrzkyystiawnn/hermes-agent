"""Lifecycle tests: durable pending queue, batching, dedup, guard, failure.

    HERMES_HOME=<tmp> PYTHONPATH=<repo> <hermes-venv>/bin/python3 test_session_end.py

Offline (LLM mocked). Exercises the REAL provider class so the lifecycle wiring
(on_session_end enqueue+drain, on_turn_start drain) is covered, not just the
extract helpers.

Design under test (D1-D5, approved):
  * on_session_end: enqueue every gated message (commit BEFORE the LLM), then drain
    one batch of EXTRACT_BATCH_SIZE (5).
  * on_turn_start: drain one more batch of 5, FIFO.
  * a row leaves temporal_pending ONLY after its extraction succeeded.
  * identity for dedup = (source_session_id, source_message_id).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract as _ex  # noqa: E402
import storage  # noqa: E402
from __init__ import TemporalMemoryProvider  # noqa: E402

TZ = ZoneInfo("Asia/Jakarta")
ANCHOR = datetime(2026, 10, 7, 10, 0, tzinfo=TZ).timestamp()

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"{'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")


def _llm(items):
    raw = json.dumps({"items": items})

    def _fn(system_prompt, user_payload):
        _fn.calls.append(user_payload)
        return raw

    _fn.calls = []
    return _fn


def _msg(text, row_id, uid, ts=ANCHOR):
    return {"role": "user", "content": text, "timestamp": ts, "message_uid": uid, "_row_id": row_id}


def _provider(tmp, context="primary", session="sess-1"):
    p = TemporalMemoryProvider()
    p.initialize(session, hermes_home=tmp, agent_context=context)
    p._tz = TZ
    return p


def _db(tmp):
    return sqlite3.connect(os.path.join(tmp, "plugin-data", "temporal", "data.db"))


def _count(tmp):
    c = _db(tmp)
    n = c.execute("SELECT COUNT(*) FROM temporal_items").fetchone()[0]
    c.close()
    return n


def _pending(tmp):
    c = _db(tmp)
    n = c.execute("SELECT COUNT(*) FROM temporal_pending").fetchone()[0]
    c.close()
    return n


def _pending_row_ids(tmp):
    c = _db(tmp)
    ids = sorted(r[0] for r in c.execute("SELECT source_message_id FROM temporal_pending"))
    c.close()
    return ids


def _item_row_ids(tmp):
    c = _db(tmp)
    ids = sorted(r[0] for r in c.execute("SELECT source_message_id FROM temporal_items"))
    c.close()
    return ids


def main() -> int:
    real = _ex._default_llm
    ok_llm = _llm([{"raw": "besok", "summary": "acara", "kind": "planned"}])
    tmp_all = []

    def use_ok():
        _ex._default_llm = ok_llm

    def restore():
        _ex._default_llm = real

    # --- 1. basic: gated messages -> items, gate miss skipped, queue emptied ---
    tmp = tempfile.mkdtemp(prefix="temporal-session-")
    tmp_all.append(tmp)
    os.environ["HERMES_HOME"] = tmp
    p = _provider(tmp)
    msgs = [
        _msg("besok gue mau makan sama ina", 11, "u11"),
        {"role": "assistant", "content": "ok"},
        _msg("dua hari lalu gue kecelakaan", 13, "u13"),
        _msg("halo apa kabar", 15, "u15"),          # gate miss -> skipped
    ]
    use_ok()
    try:
        p.on_session_end(msgs)
    finally:
        restore()
    check("basic: 2 items from 2 gated messages", _count(tmp) == 2, str(_count(tmp)))
    check("basic: queue emptied (<=batch)", _pending(tmp) == 0, str(_pending(tmp)))
    c = _db(tmp)
    rows = {r[0]: r for r in c.execute(
        "SELECT source_message_id, source_message_uid, anchor_ts, source_session_id FROM temporal_items")}
    c.close()
    check("provenance: row id 11 present", 11 in rows, str(list(rows)))
    check("provenance: row id 13 present", 13 in rows, str(list(rows)))
    check("provenance: uid carried", rows[11][1] == "u11" and rows[13][1] == "u13")
    check("provenance: anchor from message", abs(rows[11][2] - ANCHOR) < 1e-6)
    check("provenance: session id", rows[11][3] == "sess-1")

    # --- 2. idempotence: second on_session_end adds nothing (items OR pending) ---
    before_i, before_p = _count(tmp), _pending(tmp)
    use_ok()
    try:
        p.on_session_end(msgs)
    finally:
        restore()
    check("idempotence: no duplicate items", _count(tmp) == before_i, f"{before_i} -> {_count(tmp)}")
    check("idempotence: no duplicate pending", _pending(tmp) == before_p, f"{before_p} -> {_pending(tmp)}")

    # --- 3. batching: 12 candidates -> 5 items + 7 pending, then +5/2, then +2/0 ---
    tmp2 = tempfile.mkdtemp(prefix="temporal-batch-")
    tmp_all.append(tmp2)
    p2 = _provider(tmp2, session="sess-batch")
    twelve = [_msg(f"besok acara {i}", 100 + i, f"u{100+i}") for i in range(12)]

    use_ok()
    try:
        res = _ex.process_session(p2._conn, messages=twelve, session_id="sess-batch", tz=TZ)
    finally:
        restore()
    check("batch: on_session_end enqueued 12", res["enqueued"] == 12, str(res))
    check("batch: on_session_end drained 5", res["drained"] == 5, str(res))
    check("batch: 5 items stored", _count(tmp2) == 5, str(_count(tmp2)))
    check("batch: 7 pending", _pending(tmp2) == 7, str(_pending(tmp2)))
    # FIFO (D3): the FIRST five, not the newest.
    check("batch: FIFO -> oldest 5 extracted", _item_row_ids(tmp2) == list(range(100, 105)), str(_item_row_ids(tmp2)))
    check("batch: pending are the remaining 7", _pending_row_ids(tmp2) == list(range(105, 112)), str(_pending_row_ids(tmp2)))

    # lifecycle next turn: drain one batch of 5
    use_ok()
    try:
        d2 = _ex.drain_batch(p2._conn, tz=TZ)
    finally:
        restore()
    check("batch: turn 2 drained 5", d2["drained"] == 5, str(d2))
    check("batch: now 10 items", _count(tmp2) == 10, str(_count(tmp2)))
    check("batch: 2 pending left", _pending(tmp2) == 2, str(_pending(tmp2)))

    # lifecycle next turn: drain the last 2
    use_ok()
    try:
        d3 = _ex.drain_batch(p2._conn, tz=TZ)
    finally:
        restore()
    check("batch: turn 3 drained 2", d3["drained"] == 2, str(d3))
    check("batch: all 12 items present", _item_row_ids(tmp2) == list(range(100, 112)), str(_item_row_ids(tmp2)))
    check("batch: pending fully empty", _pending(tmp2) == 0, str(_pending(tmp2)))

    # --- 4. restart/durability: a NEW provider instance drains the leftover queue ---
    tmp3 = tempfile.mkdtemp(prefix="temporal-restart-")
    tmp_all.append(tmp3)
    p3 = _provider(tmp3, session="sess-restart")
    seven = [_msg(f"besok acara {i}", 200 + i, f"u{200+i}") for i in range(7)]
    use_ok()
    try:
        _ex.process_session(p3._conn, messages=seven, session_id="sess-restart", tz=TZ)
    finally:
        restore()
    check("restart: 5 drained, 2 pending before restart", _count(tmp3) == 5 and _pending(tmp3) == 2,
          f"items={_count(tmp3)} pending={_pending(tmp3)}")
    p3._conn.close()  # simulate process exit; queue is on disk
    p3b = _provider(tmp3, session="sess-restart")   # a fresh instance (new process)
    use_ok()
    try:
        d = _ex.drain_batch(p3b._conn, tz=TZ)
    finally:
        restore()
    check("restart: new instance drained the 2 leftovers", d["drained"] == 2, str(d))
    check("restart: all 7 items, 0 pending", _item_row_ids(tmp3) == list(range(200, 207)) and _pending(tmp3) == 0,
          f"ids={_item_row_ids(tmp3)} pending={_pending(tmp3)}")
    check("restart: no duplicates after restart", _count(tmp3) == 7, str(_count(tmp3)))

    # --- 5. failure semantics: one candidate fails -> stays pending, retry works ---
    tmp4 = tempfile.mkdtemp(prefix="temporal-fail-")
    tmp_all.append(tmp4)
    p4 = _provider(tmp4, session="sess-fail")
    three = [_msg(f"besok acara {i}", 300 + i, f"u{300+i}") for i in range(3)]

    calls = {"n": 0}

    def _flaky(system_prompt, user_payload):
        calls["n"] += 1
        if calls["n"] == 2:                      # second candidate fails once
            raise RuntimeError("transient provider error")
        return json.dumps({"items": [{"raw": "besok", "summary": "acara", "kind": "planned"}]})

    _ex._default_llm = _flaky
    try:
        res = _ex.process_session(p4._conn, messages=three, session_id="sess-fail", tz=TZ)
    finally:
        restore()
    check("failure: 2 drained, 1 failed", res["drained"] == 2 and res["failed"] == 1, str(res))
    check("failure: 2 items, failed one still pending", _count(tmp4) == 2 and _pending(tmp4) == 1,
          f"items={_count(tmp4)} pending={_pending(tmp4)}")
    check("failure: the FAILED row (301) is the pending one", _pending_row_ids(tmp4) == [301],
          str(_pending_row_ids(tmp4)))
    # retry on the next lifecycle succeeds
    use_ok()
    try:
        d = _ex.drain_batch(p4._conn, tz=TZ)
    finally:
        restore()
    check("failure: retry drained the kept row", d["drained"] == 1 and d["failed"] == 0, str(d))
    check("failure: all 3 items, 0 pending, no duplicates",
          sorted(_item_row_ids(tmp4)) == [300, 301, 302] and _pending(tmp4) == 0,
          f"ids={_item_row_ids(tmp4)} pending={_pending(tmp4)}")

    # --- 6. on_turn_start drains, and is a no-op when nothing is pending ---
    tmp5 = tempfile.mkdtemp(prefix="temporal-turnstart-")
    tmp_all.append(tmp5)
    p5 = _provider(tmp5, session="sess-turnstart")
    nine = [_msg(f"besok acara {i}", 400 + i, f"u{400+i}") for i in range(9)]
    use_ok()
    try:
        _ex.process_session(p5._conn, messages=nine, session_id="sess-turnstart", tz=TZ)
        check("turn_start: 5 items, 4 pending after session end", _count(tmp5) == 5 and _pending(tmp5) == 4,
              f"items={_count(tmp5)} pending={_pending(tmp5)}")
        p5.on_turn_start(1, "halo")              # drains a batch
        check("turn_start: drained one batch -> 9 items, 0 pending",
              _count(tmp5) == 9 and _pending(tmp5) == 0, f"items={_count(tmp5)} pending={_pending(tmp5)}")
        p5.on_turn_start(2, "halo lagi")         # nothing pending -> no-op
        check("turn_start: no-op when empty", _count(tmp5) == 9 and _pending(tmp5) == 0)
    finally:
        restore()

    # --- 7. guard: cron/subagent/flush do nothing ---
    for ctx in ("cron", "subagent", "flush"):
        t = tempfile.mkdtemp(prefix=f"temporal-{ctx}-")
        tmp_all.append(t)
        pc = _provider(t, context=ctx, session="sess-x")
        use_ok()
        try:
            pc.on_session_end([_msg("besok gue pergi", 1, "u1")])
            pc.on_turn_start(1, "besok gue pergi")
        finally:
            restore()
        check(f"guard[{ctx}]: no items", _count(t) == 0, str(_count(t)))
        check(f"guard[{ctx}]: no pending", _pending(t) == 0, str(_pending(t)))
        check(f"guard[{ctx}]: sync_turn is a no-op", (pc.sync_turn("besok gue pergi", "ok"), True)[1])

    # --- 8. fail-open: LLM raising / bad JSON keep the row PENDING, no raise ---
    tmp6 = tempfile.mkdtemp(prefix="temporal-fo-")
    tmp_all.append(tmp6)
    p6 = _provider(tmp6, session="sess-fo")

    def _boom(s, u):
        raise RuntimeError("down")

    _ex._default_llm = _boom
    try:
        p6.on_session_end([_msg("besok gue pergi", 21, "u21")])
    finally:
        restore()
    check("fail-open: LLM exception -> 0 items, 1 pending (kept)", _count(tmp6) == 0 and _pending(tmp6) == 1,
          f"items={_count(tmp6)} pending={_pending(tmp6)}")

    _ex._default_llm = lambda s, u: "not json"
    try:
        p6.on_session_end([_msg("besok gue pergi", 23, "u23")])
    finally:
        restore()
    check("fail-open: invalid JSON -> 0 items, 2 pending (kept)", _count(tmp6) == 0 and _pending(tmp6) == 2,
          f"items={_count(tmp6)} pending={_pending(tmp6)}")

    # None/[] must not raise. on_session_end still drains a batch (D4), so keep the
    # LLM failing: the two rows then stay pending (fail-open, work preserved).
    _ex._default_llm = _boom
    try:
        p6.on_session_end(None)
        p6.on_session_end([])
    finally:
        restore()
    check("fail-open: None/[] messages safe, queue preserved", _pending(tmp6) == 2,
          str(_pending(tmp6)))

    # --- 9. no timestamp / no row id still stores ---
    tmp7 = tempfile.mkdtemp(prefix="temporal-noprov-")
    tmp_all.append(tmp7)
    p7 = _provider(tmp7, session="sess-np")
    use_ok()
    try:
        p7.on_session_end([{"role": "user", "content": "besok gue pergi"}])
    finally:
        restore()
    check("no-provenance: still stores", _count(tmp7) == 1, str(_count(tmp7)))

    for t in tmp_all:
        shutil.rmtree(t, ignore_errors=True)
    print(f"\n{'ALL PASSED' if _failures == 0 else str(_failures) + ' FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
