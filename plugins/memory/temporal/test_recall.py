"""Recall tests — offline, no LLM (retrieval is deterministic).

    HERMES_HOME=<tmp> PYTHONPATH=<repo> <hermes-venv>/bin/python3 test_recall.py

Covers the Fase 1 langkah 4 contract: near-term window, historical resolution,
stale/status/ambiguity/confidence handling, caps, fail-open, and the D1 note that
the snapshot is bounded while total replay stays O(n^2).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import recall  # noqa: E402
import storage  # noqa: E402

TZ = ZoneInfo("Asia/Jakarta")
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=TZ).timestamp()

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"{'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")


def _day(offset_days: float, hour: int = 10) -> float:
    """Epoch for a WIB calendar day at `hour` local, offset from the anchor date."""
    base = datetime(2026, 10, 7, hour, 0, tzinfo=TZ)
    return (base + timedelta(days=offset_days)).timestamp()


def _item(conn, *, start, end, summary, session="s", mid=None, status="active",
          stale=False, ambiguous=False, confidence=1.0, kind="planned", epistemic="factual",
          precision="hari", raw=None):
    return storage.insert_item(conn, {
        "raw_expression": raw or summary, "summary": summary,
        "time_start": start, "time_end": end, "precision": precision,
        "ambiguous": ambiguous, "kind": kind, "status": status, "epistemic": epistemic,
        "confidence": confidence, "anchor_ts": NOW,
        "source_session_id": session, "source_message_id": mid,
        "stale": stale, "stale_reason": "test" if stale else None,
    })


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="temporal-recall-")
    os.environ["HERMES_HOME"] = tmp
    conn = sqlite3.connect(os.path.join(tmp, "r.db"))
    storage.init_schema(conn)

    class _Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("gone")

    # --- near-term: inside +/-7d shows, outside does not ---
    _item(conn, start=_day(1), end=_day(1, 23), summary="makan sama Ina", mid=1)
    _item(conn, start=_day(-3), end=_day(-3, 23), summary="kecelakaan motor", mid=2, kind="occurred")
    _item(conn, start=_day(30), end=_day(30, 23), summary="jauh di depan", mid=3)      # outside
    _item(conn, start=_day(-30), end=_day(-30, 23), summary="jauh di belakang", mid=4)  # outside
    block = recall.build_context(conn, query="halo", tz=TZ, now=NOW)
    check("near-term: inside window shown", "makan sama Ina" in block and "kecelakaan motor" in block, block)
    check("near-term: outside window hidden", "jauh di depan" not in block and "jauh di belakang" not in block, block)
    check("near-term: header present", block.startswith("Temporal memory"), block[:60])
    check("near-term: absolute date in line", "2026-10-08" in block and "2026-10-04" in block, block)

    # --- boundary: exactly +/-7 days is inclusive ---
    _item(conn, start=_day(7), end=_day(7, 23), summary="tepat batas depan", mid=5)
    _item(conn, start=_day(-7), end=_day(-7, 23), summary="tepat batas belakang", mid=6)
    block = recall.build_context(conn, query="halo", tz=TZ, now=NOW)
    check("boundary: +7d inclusive", "tepat batas depan" in block, block)
    check("boundary: -7d inclusive", "tepat batas belakang" in block, block)

    # --- interval overlap: a multi-day span touching the window edge shows ---
    _item(conn, start=_day(-20), end=_day(-5), summary="proyek panjang", mid=7)
    block = recall.build_context(conn, query="halo", tz=TZ, now=NOW)
    check("overlap: span ending inside window shows", "proyek panjang" in block, block)

    # --- stale is skipped ---
    _item(conn, start=_day(2), end=_day(2, 23), summary="item stale", mid=8, stale=True)
    block = recall.build_context(conn, query="halo", tz=TZ, now=NOW)
    check("stale: skipped by default", "item stale" not in block, block)

    # --- cancelled/superseded skipped by default, shown when explicitly asked ---
    _item(conn, start=_day(2), end=_day(2, 23), summary="acara dibatalkan", mid=9, status="cancelled")
    block = recall.build_context(conn, query="halo", tz=TZ, now=NOW)
    check("cancelled: skipped by default", "acara dibatalkan" not in block, block)
    block = recall.build_context(conn, query="apa yang batal kemarin?", tz=TZ, now=NOW)
    check("cancelled: shown when asked", "acara dibatalkan" in block and "[cancelled]" in block, block)

    # --- ambiguity + low confidence markers ---
    _item(conn, start=_day(3), end=_day(3, 23), summary="jumat depan meeting", mid=10,
          ambiguous=True, confidence=0.3)
    block = recall.build_context(conn, query="halo", tz=TZ, now=NOW)
    check("ambiguity: marker present", "[ambigu]" in block, block)
    check("low confidence: marker present", "[?]" in block, block)

    # --- historical: expression resolves to an absolute interval ---
    _item(conn, start=_day(-2), end=_day(-2, 23), summary="kecelakaan lama", mid=11, kind="occurred")
    _item(conn, start=_day(-400), end=_day(-400, 23), summary="kejadian setahun lalu", mid=12)
    block = recall.build_context(conn, query="apa yang terjadi dua hari lalu?", tz=TZ, now=NOW)
    check("historical: resolved target surfaces", "kecelakaan lama" in block, block)
    check("historical: distant item not in ±7 window but in resolved target? (not here)",
          "kejadian setahun lalu" not in block, block)

    # --- historical with an explicit date ---
    _item(conn, start=_day(-370), end=_day(-370, 23), summary="pindah rumah tahun lalu", mid=13)
    block = recall.build_context(conn, query="masih ingat tanggal 2 oktober 2025?", tz=TZ, now=NOW)
    check("historical: explicit date resolves", "pindah rumah tahun lalu" in block, block)

    # --- historical request WITHOUT a resolvable target -> bounded fallback ---
    _item(conn, start=_day(-100), end=_day(-100, 23), summary="kejadian 100 hari lalu", mid=14)
    block = recall.build_context(conn, query="apa aja yang pernah gue ceritain dulu?", tz=TZ, now=NOW)
    check("historical fallback: bounded lookback surfaces older item", "kejadian 100 hari lalu" in block, block)

    # --- D6: a bare past word WITHOUT a retrieval ask is NOT historical ---
    _item(conn, start=_day(-100), end=_day(-100, 23), summary="hal 100 hari lalu", mid=15)
    block = recall.build_context(conn, query="kemarin gue capek banget", tz=TZ, now=NOW)
    check("D6: past statement is not a recall request", "hal 100 hari lalu" not in block, block)
    check("D6: is_historical_request false for statement", recall.is_historical_request("kemarin gue capek") is False)
    check("D6: is_historical_request true for question",
          recall.is_historical_request("apa yang terjadi kemarin?") is True)

    # --- cap: 8 items max ---
    tmp2 = tempfile.mkdtemp(prefix="temporal-recall-cap-")
    conn2 = sqlite3.connect(os.path.join(tmp2, "r.db"))
    storage.init_schema(conn2)
    for i in range(20):
        _item(conn2, start=_day(0, 8 + i % 10), end=_day(0, 8 + i % 10), summary=f"acara {i}", mid=100 + i)
    block = recall.build_context(conn2, query="halo", tz=TZ, now=NOW)
    n = sum(1 for ln in block.splitlines() if ln.startswith("- "))
    check("cap: at most 8 items", n <= recall.MAX_ITEMS, f"{n}")

    # --- character cap: many long summaries stay <= MAX_CHARS ---
    tmp3 = tempfile.mkdtemp(prefix="temporal-recall-chars-")
    conn3 = sqlite3.connect(os.path.join(tmp3, "r.db"))
    storage.init_schema(conn3)
    for i in range(20):
        _item(conn3, start=_day(0, 8 + i % 10), end=_day(0, 8 + i % 10),
              summary=("ringkasan sangat panjang " * 10) + str(i), mid=200 + i)
    block = recall.build_context(conn3, query="halo", tz=TZ, now=NOW)
    check("char cap: block <= MAX_CHARS", len(block) <= recall.MAX_CHARS, f"{len(block)}")
    check("char cap: no truncated mid-line", all(ln.startswith("- ") for ln in block.splitlines()[1:]), block)

    # --- no items -> "" ---
    tmp4 = tempfile.mkdtemp(prefix="temporal-recall-empty-")
    conn4 = sqlite3.connect(os.path.join(tmp4, "r.db"))
    storage.init_schema(conn4)
    check("empty: no items -> ''", recall.build_context(conn4, query="halo", tz=TZ, now=NOW) == "")

    # --- fail-open: broken conn -> "" ---
    check("fail-open: broken conn -> ''", recall.build_context(_Broken(), query="halo", tz=TZ, now=NOW) == "")
    check("fail-open: None conn -> ''", recall.build_context(None, query="halo", tz=TZ, now=NOW) == "")
    check("count_items matches lines",
          recall.count_items(conn, query="halo", tz=TZ, now=NOW) ==
          sum(1 for ln in recall.build_context(conn, query="halo", tz=TZ, now=NOW).splitlines() if ln.startswith("- ")))

    # --- recall_range (explicit tool): resolve expression -> items ---
    rr = recall.recall_range(conn, expression="dua hari lalu", tz=TZ, now=NOW)
    check("recall_range: matched", rr["matched"] is True, str(rr))
    check("recall_range: finds the past item", any("kecelakaan lama" in it["summary"] for it in rr["items"]), str(rr["items"]))
    check("recall_range: provenance carried", any(it["source_message_id"] == 11 for it in rr["items"]), str(rr["items"]))
    rr2 = recall.recall_range(conn, expression="tanggal 2 oktober 2025", tz=TZ, now=NOW)
    check("recall_range: explicit date matched", rr2["matched"] and any("pindah rumah" in it["summary"] for it in rr2["items"]), str(rr2["items"]))
    rr3 = recall.recall_range(conn, expression="kapan-kapan", tz=TZ, now=NOW)
    check("recall_range: unresolvable -> matched False", rr3["matched"] is False and rr3["items"] == [])
    # A broken conn: resolution still succeeds (matched=True) but the item query
    # fails open -> empty items, no raise.
    broken = recall.recall_range(_Broken(), expression="kemarin", tz=TZ, now=NOW)
    check("recall_range: broken conn -> fail-open, no raise, empty items",
          isinstance(broken, dict) and broken["items"] == [], str(broken))

    # --- D1 note: bounded snapshot, but replay is O(n^2) (documented, measured) ---
    # The NEW block per turn is bounded; the core still replays each sidecar. We assert
    # the bound holds regardless of how many items exist (snapshot size is stable).
    sizes = []
    for extra in range(0, 6):
        _item(conn, start=_day(0, 18 + extra), end=_day(0, 18 + extra), summary=f"tambahan {extra}", mid=300 + extra)
        sizes.append(len(recall.build_context(conn, query="halo", tz=TZ, now=NOW)))
    check("D1: per-turn snapshot size stays <= MAX_CHARS as DB grows",
          all(s <= recall.MAX_CHARS for s in sizes), str(sizes))

    for t in (tmp, tmp2, tmp3, tmp4):
        shutil.rmtree(t, ignore_errors=True)
    print(f"\n{'ALL PASSED' if _failures == 0 else str(_failures) + ' FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
