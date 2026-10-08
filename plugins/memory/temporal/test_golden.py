"""Golden table for the temporal resolver — runs standalone (no pytest needed).

    python test_golden.py

Anchor: Rabu 2026-10-07 10:00 WIB (Asia/Jakarta). Matches the PLAN's golden table.
Every row asserts (start_date, precision) and, where the PLAN marks "?", that the
resolver RETURNS the ambiguity instead of silently picking.
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from resolver import P_DAY, P_MONTH, P_PART, P_WEEK, resolve

TZ = ZoneInfo("Asia/Jakarta")
ANCHOR = datetime(2026, 10, 7, 10, 0, tzinfo=TZ)

# (expression, expected_start_date, expected_precision, expect_ambiguous)
GOLDEN = [
    # PLAN rows
    ("hari ini", "2026-10-07", P_DAY, False),
    ("besok", "2026-10-08", P_DAY, False),
    ("lusa", "2026-10-09", P_DAY, False),
    ("kemarin", "2026-10-06", P_DAY, False),
    ("kemarin lusa", "2026-10-05", P_DAY, False),
    ("dua hari lalu", "2026-10-05", P_DAY, False),
    ("tadi malam", "2026-10-06", P_PART, False),
    ("besok sore", "2026-10-08", P_PART, False),
    ("minggu depan", "2026-10-12", P_WEEK, False),
    ("minggu lalu", "2026-09-28", P_WEEK, False),
    ("bulan lalu", "2026-09-01", P_MONTH, False),
    ("tanggal 12 september", "2026-09-12", P_DAY, False),
    ("jumat depan", "2026-10-09", P_DAY, True),      # D5: nearest, but ambiguous
    ("tanggal 12", "2026-10-12", P_DAY, True),        # no month -> ambiguous
    ("12/9", "2026-09-12", P_DAY, False),
    ("bsk", "2026-10-08", P_DAY, False),
    ("kmrn", "2026-10-06", P_DAY, False),
    # extra coverage
    ("bulan depan", "2026-11-01", P_MONTH, False),
    ("tiga hari lagi", "2026-10-10", P_DAY, False),
    ("seminggu lalu", None, "", False),               # "seminggu" not yet supported -> unmatched
    ("halo apa kabar", None, "", False),              # not temporal -> unmatched
    ("", None, "", False),
]

# Cross-timezone: the anchor's calendar day is decided in the TARGET zone.
# UTC 2026-10-07T20:00 == 2026-10-08T03:00 WIB, so "hari ini" is the 8th there.
CROSS_TZ = [
    (datetime(2026, 10, 7, 20, 0, tzinfo=ZoneInfo("UTC")), ZoneInfo("Asia/Jakarta"), "hari ini", "2026-10-08"),
    (datetime(2026, 10, 7, 20, 0, tzinfo=ZoneInfo("UTC")), ZoneInfo("Asia/Jakarta"), "besok", "2026-10-09"),
    (datetime(2026, 10, 7, 20, 0, tzinfo=ZoneInfo("UTC")), ZoneInfo("UTC"), "besok", "2026-10-08"),
]


def _run() -> int:
    if ANCHOR.weekday() != 2:
        print(f"FATAL: anchor is weekday {ANCHOR.weekday()}, expected 2 (Wednesday)")
        return 2
    failures = 0
    for expr, want_date, want_prec, want_amb in GOLDEN:
        r = resolve(expr, ANCHOR, tz=TZ)
        if want_date is None:
            ok = not r.matched
            got = "(unmatched)"
        else:
            ok = (
                r.matched
                and r.start.strftime("%Y-%m-%d") == want_date
                and r.precision == want_prec
                and r.ambiguous == want_amb
            )
            got = (
                f"{r.start.strftime('%Y-%m-%d')} prec={r.precision} amb={r.ambiguous}"
                if r.matched else "(unmatched)"
            )
        status = "ok  " if ok else "FAIL"
        if not ok:
            failures += 1
        want = "(unmatched)" if want_date is None else f"{want_date} prec={want_prec} amb={want_amb}"
        print(f"{status} {expr!r:24} want={want:34} got={got}")

    for anchor, tz, expr, want_date in CROSS_TZ:
        r = resolve(expr, anchor, tz=tz)
        got = r.start.strftime("%Y-%m-%d") if r.matched else "(unmatched)"
        ok = got == want_date
        if not ok:
            failures += 1
        label = f"{expr!r} @{tz.key}"
        print(f"{'ok  ' if ok else 'FAIL'} {label:24} want={want_date:34} got={got}")

    total = len(GOLDEN) + len(CROSS_TZ)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
