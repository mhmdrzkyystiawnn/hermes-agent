"""Extraction tests — standalone, offline (LLM is mocked).

    HERMES_HOME=<tmp> PYTHONPATH=<repo> <hermes-venv>/bin/python3 test_extract.py

Covers: gate, JSON parsing tolerance, LLM-item coercion, resolve wiring,
fail-open on every failure path, and the full pipeline into storage.
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

import extract  # noqa: E402
import storage  # noqa: E402

TZ = ZoneInfo("Asia/Jakarta")
ANCHOR = datetime(2026, 10, 7, 10, 0, tzinfo=TZ).timestamp()
ANCHOR_ISO = "2026-10-07"

_failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"{'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")


def _llm_returning(payload: dict | str):
    """Mock LLM that returns a fixed JSON payload (or raw string)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    def _fn(system_prompt: str, user_payload: str) -> str:
        _fn.calls.append((system_prompt, user_payload))
        return raw

    _fn.calls = []
    return _fn


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="temporal-extract-")
    os.environ["HERMES_HOME"] = tmp

    # --- GATE (no LLM) ---
    positives = [
        "besok gue mau makan sama ina", "dua hari lalu gue kecelakaan",
        "tanggal 12 september gue ada acara", "minggu depan ke bogor",
        "tadi malam kurang tidur", "jumat kita meeting", "12/9 gue libur",
        "nanti sore gue telpon", "lagi sakit sejak kemarin", "bulan lalu gue ke bali",
    ]
    negatives = [
        "halo apa kabar", "tolong perbaiki bug ini", "kode nya error",
        "gue suka kopi", "terima kasih", "",
    ]
    for text in positives:
        check(f"gate hit: {text[:28]!r}", extract.gate(text) is True)
    for text in negatives:
        check(f"gate miss: {text[:28]!r}", extract.gate(text) is False)

    # --- gate=false never calls the LLM ---
    llm = _llm_returning({"items": [{"raw": "besok", "summary": "x"}]})
    out = extract.extract_items("halo apa kabar", anchor_ts=ANCHOR, tz=TZ, llm=llm)
    check("gate miss -> no LLM call", len(llm.calls) == 0 and out == [])

    # --- happy path: LLM -> resolve -> storage-ready dicts ---
    llm = _llm_returning({"items": [
        {"raw": "besok", "summary": "makan sama Ina", "kind": "planned",
         "epistemic": "factual", "confidence": 0.9},
        {"raw": "minggu depan", "summary": "ke Bogor", "kind": "planned",
         "epistemic": "factual", "confidence": 0.8},
    ]})
    items = extract.extract_items("besok gue mau makan sama ina, minggu depan ke bogor",
                                  anchor_ts=ANCHOR, tz=TZ, llm=llm)
    check("happy path: 2 items", len(items) == 2, str(len(items)))
    d0 = datetime.fromtimestamp(items[0]["time_start"], tz=TZ).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(items[1]["time_start"], tz=TZ).strftime("%Y-%m-%d")
    check("resolved 'besok' -> 2026-10-08", d0 == "2026-10-08", d0)
    check("resolved 'minggu depan' -> 2026-10-12", d1 == "2026-10-12", d1)
    check("precision carried", items[0]["precision"] == "hari" and items[1]["precision"] == "minggu")
    check("anchor_ts stamped", items[0]["anchor_ts"] == ANCHOR)
    check("tz recorded", items[0]["resolved_tz"] == "Asia/Jakarta", items[0]["resolved_tz"])
    check("confidence carried", items[0]["confidence"] == 0.9)

    # --- LLM says "no items" ---
    llm = _llm_returning({"items": []})
    check("empty items -> no rows", extract.extract_items("besok gue pergi", anchor_ts=ANCHOR, tz=TZ, llm=llm) == [])

    # --- parsing tolerance ---
    fenced = "```json\n" + json.dumps({"items": [{"raw": "besok", "summary": "x"}]}) + "\n```"
    items = extract.extract_items("besok pergi", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning(fenced))
    check("parse: markdown fence stripped", len(items) == 1)
    noisy = "Sure! Here you go: " + json.dumps({"items": [{"raw": "lusa", "summary": "x"}]}) + " hope that helps"
    items = extract.extract_items("lusa pergi", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning(noisy))
    check("parse: prose around JSON tolerated", len(items) == 1)
    check("parse: garbage -> []", extract.extract_items("besok", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning("not json at all")) == [])
    check("parse: non-dict JSON -> []", extract.extract_items("besok", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning("[1,2,3]")) == [])

    # --- coercion ---
    items = extract.extract_items("besok pergi", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning({"items": [
        {"raw": "besok", "summary": "s", "kind": "BOGUS", "epistemic": "nope", "confidence": 5},
    ]}))
    check("coerce: bad kind -> planned", items[0]["kind"] == "planned")
    check("coerce: bad epistemic -> factual", items[0]["epistemic"] == "factual")
    check("coerce: confidence clamped to 1.0", items[0]["confidence"] == 1.0)
    items = extract.extract_items("besok pergi", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning({"items": [
        {"raw": "besok", "summary": "s", "confidence": -3},
    ]}))
    check("coerce: negative confidence clamped to 0.0", items[0]["confidence"] == 0.0)

    # --- unresolvable expression is dropped (resolver is the authority) ---
    items = extract.extract_items("besok pergi", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning({"items": [
        {"raw": "kapan-kapan", "summary": "s"},
        {"raw": "besok", "summary": "keep me"},
    ]}))
    check("unresolvable raw dropped, resolvable kept", len(items) == 1 and items[0]["summary"] == "keep me", str(items))

    # --- max items cap ---
    many = {"items": [{"raw": "besok", "summary": f"s{i}"} for i in range(10)]}
    items = extract.extract_items("besok", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning(many))
    check("cap at MAX_ITEMS_PER_TURN", len(items) == extract.MAX_ITEMS_PER_TURN, str(len(items)))

    # --- LLM raising is fail-open ---
    def _boom(_s, _u):
        raise RuntimeError("provider down")

    check("LLM exception -> [] (fail-open)", extract.extract_items("besok", anchor_ts=ANCHOR, tz=TZ, llm=_boom) == [])

    # --- ambiguous expression carries alternatives ---
    items = extract.extract_items("jumat depan meeting", anchor_ts=ANCHOR, tz=TZ, llm=_llm_returning({"items": [
        {"raw": "jumat depan", "summary": "meeting"},
    ]}))
    check("ambiguous flag stored", items[0]["ambiguous"] is True)
    check("alternatives stored", items[0]["alternatives"] and len(items[0]["alternatives"]) == 1)

    # --- FULL PIPELINE into storage (mock LLM) ---
    conn = sqlite3.connect(os.path.join(tmp, "pipe.db"))
    storage.init_schema(conn)
    messages = [
        {"role": "user", "content": "besok gue makan sama ina", "timestamp": ANCHOR,
         "message_uid": "uid-1", "_row_id": 7},
        {"role": "assistant", "content": "siap"},
    ]
    n = extract.run_extraction(
        conn, user_text="besok gue makan sama ina", messages=messages, session_id="s-1", tz=TZ,
        llm=_llm_returning({"items": [{"raw": "besok", "summary": "makan sama Ina", "kind": "planned"}]}),
    )
    check("pipeline: 1 row stored", n == 1, str(n))
    rows = storage.items_in_window(conn, ANCHOR, ANCHOR + 3 * 86400)
    check("pipeline: row in window", len(rows) == 1)
    row = rows[0]
    check("pipeline: provenance uid from message", row["source_message_uid"] == "uid-1", str(row["source_message_uid"]))
    check("pipeline: provenance row id from message", row["source_message_id"] == 7, str(row["source_message_id"]))
    check("pipeline: anchor = message timestamp", row["anchor_ts"] == ANCHOR)
    check("pipeline: session id", row["source_session_id"] == "s-1")

    # --- provenance fallback: no timestamp/uid on the row ---
    n = extract.run_extraction(
        conn, user_text="lusa pergi", messages=[{"role": "user", "content": "lusa pergi"}],
        session_id="s-2", tz=TZ, now_ts=ANCHOR,
        llm=_llm_returning({"items": [{"raw": "lusa", "summary": "pergi"}]}),
    )
    check("pipeline: stores without provenance", n == 1)
    got = [r for r in storage.items_in_window(conn, ANCHOR, ANCHOR + 5 * 86400) if r["source_session_id"] == "s-2"][0]
    check("pipeline: anchor falls back to now_ts", got["anchor_ts"] == ANCHOR)
    check("pipeline: uid/id are NULL", got["source_message_uid"] is None and got["source_message_id"] is None)

    # --- empty user text is a no-op ---
    check("pipeline: empty text -> 0", extract.run_extraction(
        conn, user_text="   ", messages=[], session_id="s-3", tz=TZ, llm=_llm_returning({"items": []})) == 0)

    # --- never raises even with a broken conn ---
    class _Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("db gone")

    check("pipeline: broken conn -> 0, no raise", extract.run_extraction(
        _Broken(), user_text="besok", messages=[{"role": "user", "content": "besok"}],
        session_id="s-4", tz=TZ, llm=_llm_returning({"items": [{"raw": "besok", "summary": "x"}]})) == 0)

    conn.close()
    check("db inside temp home", os.path.exists(os.path.join(tmp, "pipe.db")))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASSED' if _failures == 0 else str(_failures) + ' FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
