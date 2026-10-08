"""Pure Indonesian date-expression resolver for the temporal memory provider.

No I/O, no network, no LLM. Deterministic: ``(expression, anchor, tz) -> Resolution``.

Design rules (from Fase 0):
  * The LLM must never compute a date. It may only normalise an expression; the
    arithmetic happens here, in code, against an explicit anchor.
  * Ambiguity is RETURNED, not hidden. Expressions the plan marks "?" come back
    with ``ambiguous=True`` plus ``alternatives``, so the caller can decide.
  * Pure function: same inputs -> same output. The anchor is always passed in
    (never read from the clock), which makes the golden table testable.

Open product decisions are surfaced as module constants below, not buried in
branches. Flip a constant, re-run the golden test, done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Tunables — product decisions, surfaced deliberately (see PLAN: D5/D6/D7)
# ---------------------------------------------------------------------------

# D7: which day starts a week. Iki's proposal: Monday.
WEEK_STARTS_ON_MONDAY = True

# D5: "jumat depan" -> True = the nearest upcoming Friday (9 Oct for a Wed 7 Oct
# anchor); False = the Friday of next week (16 Oct). Either way the resolver
# flags the result ambiguous and offers the other candidate.
WEEKDAY_DEPAN_IS_NEAREST = True

# "tanggal 12" with no month -> True = nearest FUTURE 12th; False = nearest PAST.
BARE_DAY_IS_FUTURE = True

# Part-of-day boundaries: (start_hour, end_hour_exclusive). "malam" runs to 24.
PART_OF_DAY: dict[str, Tuple[int, int]] = {
    "subuh": (3, 5),
    "pagi": (4, 11),
    "siang": (11, 15),
    "sore": (15, 18),
    "malam": (18, 24),
}

# Precision labels (match the PLAN golden table's wording).
P_DAY = "hari"
P_PART = "bagian hari"
P_WEEK = "minggu"
P_MONTH = "bulan"
P_YEAR = "tahun"

WEEKDAYS = {
    "senin": 0, "selasa": 1, "rabu": 2, "kamis": 3,
    "jumat": 4, "jum'at": 4, "jumaat": 4,
    "sabtu": 5, "minggu": 6, "ahad": 6,
}

MONTHS = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2,
    "maret": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mei": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "agustus": 8, "agu": 8, "ags": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "desember": 12, "des": 12,
}

NUM_WORDS = {
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6,
    "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11,
    "duabelas": 12, "dua belas": 12,
}

_ABBREV = (
    (r"\bbsk\b", "besok"),
    (r"\bkmrn\b", "kemarin"),
    (r"\bkmaren\b", "kemarin"),
    (r"\bhr\b", "hari"),
    (r"\bhr ini\b", "hari ini"),
    (r"\bminggu dpn\b", "minggu depan"),
    (r"\bminggu lalulalu\b", "minggu lalu"),
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """A resolved time reference. ``start``/``end`` are tz-aware and INCLUSIVE."""

    matched: bool
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    precision: str = ""
    ambiguous: bool = False
    alternatives: List[Tuple[datetime, datetime]] = field(default_factory=list)
    raw: str = ""
    note: str = ""

    def __repr__(self) -> str:  # compact, for test output
        if not self.matched:
            return f"Resolution(unmatched, raw={self.raw!r})"
        return (
            f"Resolution({self.start:%Y-%m-%d %H:%M:%S}..{self.end:%Y-%m-%d %H:%M:%S}"
            f", {self.precision}{', ambiguous' if self.ambiguous else ''})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(expression: str) -> str:
    text = (expression or "").strip().lower()
    for pattern, replacement in _ABBREV:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[,.]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _num(token: str) -> Optional[int]:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return NUM_WORDS.get(token)


def _day_range(d: date, tz) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0, 0), tzinfo=tz)
    return start, start + timedelta(days=1) - timedelta(microseconds=1)


def _part_range(d: date, part: str, tz) -> Tuple[datetime, datetime]:
    h0, h1 = PART_OF_DAY[part]
    start = datetime.combine(d, time(h0, 0, 0), tzinfo=tz)
    end = datetime.combine(d, time(0, 0, 0), tzinfo=tz) + timedelta(hours=h1) - timedelta(microseconds=1)
    return start, end


def _week_range(d: date, tz, offset_weeks: int = 0) -> Tuple[datetime, datetime]:
    wd = d.weekday()  # Monday == 0
    if not WEEK_STARTS_ON_MONDAY:
        wd = (wd + 1) % 7  # Sunday == 0
    monday = d - timedelta(days=wd) + timedelta(weeks=offset_weeks)
    start = datetime.combine(monday, time(0, 0, 0), tzinfo=tz)
    return start, start + timedelta(days=7) - timedelta(microseconds=1)


def _month_range(year: int, month: int, tz, offset: int = 0) -> Tuple[datetime, datetime]:
    idx = (year * 12 + (month - 1)) + offset
    y, m = divmod(idx, 12)
    m += 1
    start = datetime.combine(date(y, m, 1), time(0, 0, 0), tzinfo=tz)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return start, datetime.combine(nxt, time(0, 0, 0), tzinfo=tz) - timedelta(microseconds=1)


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _upcoming_weekday(anchor: date, target_wd: int) -> date:
    delta = (target_wd - anchor.weekday()) % 7
    return anchor + timedelta(days=delta)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve(expression: str, anchor: datetime, tz=None) -> Resolution:
    """Resolve *expression* against *anchor*.

    ``anchor`` MUST be tz-aware — the whole point is that the anchor's timezone
    decides the calendar day (a message at 01:00 WIB belongs to that WIB day).
    """
    if anchor is None or anchor.tzinfo is None:
        raise ValueError("anchor must be a tz-aware datetime")
    tz = tz or anchor.tzinfo
    text = _normalize(expression)
    if not text:
        return Resolution(False, raw=expression or "")

    # The anchor's CALENDAR DAY is decided in the target timezone, not in the
    # anchor's own zone: a UTC 2026-10-07T20:00 anchor is already 2026-10-08 in
    # Asia/Jakarta, so "besok" there means 2026-10-09.
    today = anchor.astimezone(tz).date()

    def _ok(start, end, precision, **kw):
        return Resolution(True, start, end, precision, raw=text, **kw)

    # -- 1. "tadi malam" (yesterday's night) — before bare "malam" ------------
    if re.search(r"\btadi\s+malam\b", text):
        s, e = _part_range(today - timedelta(days=1), "malam", tz)
        return _ok(s, e, P_PART, note="tadi malam = malam kemarin")

    # -- 2. "kemarin lusa" — before bare "kemarin" ---------------------------
    if re.search(r"\bkemarin\s+lusa\b", text):
        s, e = _day_range(today - timedelta(days=2), tz)
        return _ok(s, e, P_DAY)

    # -- 3. explicit date with month name: "12 september", "12 sep 2026" ------
    m = re.search(r"\b(\d{1,2})\s+([a-z']+)(?:\s+(\d{4}))?\b", text)
    if m and m.group(2) in MONTHS:
        day, month = int(m.group(1)), MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else today.year
        d = _safe_date(year, month, day)
        if d is not None:
            s, e = _day_range(d, tz)
            return _ok(s, e, P_DAY, note="tahun berjalan" if not m.group(3) else "")

    # -- 4. numeric d/m[/y]: "12/9", "12-9-2026" ------------------------------
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = today.year
        if m.group(3):
            year = int(m.group(3))
            if year < 100:
                year += 2000
        d = _safe_date(year, month, day)
        if d is not None:
            s, e = _day_range(d, tz)
            return _ok(s, e, P_DAY, note="format d/m")

    # -- 5. "tanggal 12" (no month) — ambiguous ------------------------------
    m = re.search(r"\btanggal\s+(\d{1,2})\b", text)
    if m:
        day = int(m.group(1))
        candidates: List[date] = []
        for delta in range(0, 400):
            probe = today + timedelta(days=delta) if BARE_DAY_IS_FUTURE else today - timedelta(days=delta)
            if probe.day == day:
                candidates.append(probe)
                break
        for delta in range(1, 400):
            probe = today - timedelta(days=delta) if BARE_DAY_IS_FUTURE else today + timedelta(days=delta)
            if probe.day == day:
                candidates.append(probe)
                break
        if candidates:
            primary = _day_range(candidates[0], tz)
            alts = [_day_range(c, tz) for c in candidates[1:]]
            return _ok(primary[0], primary[1], P_DAY, ambiguous=True, alternatives=alts,
                       note="tanpa bulan; default = terdekat ke depan")

    # -- 6. "N <unit> lalu/lagi/yang lalu" -----------------------------------
    m = re.search(r"\b(\d{1,2}|[a-z]+(?:\s+[a-z]+)?)\s+(hari|minggu|bulan|tahun)\s+(?:yang\s+)?(lalu|lagi|kemudian|ke depan|kedepan)\b", text)
    if m:
        n = _num(m.group(1))
        unit, direction = m.group(2), m.group(3)
        if n is not None:
            past = direction in ("lalu", "kemudian")
            sign = -1 if past else 1
            if unit == "hari":
                s, e = _day_range(today + timedelta(days=sign * n), tz)
                return _ok(s, e, P_DAY)
            if unit == "minggu":
                s, e = _week_range(today, tz, offset_weeks=sign * n)
                return _ok(s, e, P_WEEK)
            if unit == "bulan":
                s, e = _month_range(today.year, today.month, tz, offset=sign * n)
                return _ok(s, e, P_MONTH)
            if unit == "tahun":
                y = today.year + sign * n
                s = datetime.combine(date(y, 1, 1), time(0, 0, 0), tzinfo=tz)
                e = datetime.combine(date(y, 12, 31), time(0, 0, 0), tzinfo=tz) + timedelta(days=1) - timedelta(microseconds=1)
                return _ok(s, e, P_YEAR)

    # -- 7. "minggu depan" / "minggu lalu" — MUST run before the bare weekday
    #       rule, because "minggu" is also the word for Sunday (WEEKDAYS).
    if re.search(r"\bminggu\s+depan\b", text):
        s, e = _week_range(today, tz, offset_weeks=1)
        return _ok(s, e, P_WEEK)
    if re.search(r"\bminggu\s+lalu\b", text):
        s, e = _week_range(today, tz, offset_weeks=-1)
        return _ok(s, e, P_WEEK)

    # -- 8. "bulan depan" / "bulan lalu" -------------------------------------
    if re.search(r"\bbulan\s+depan\b", text):
        s, e = _month_range(today.year, today.month, tz, offset=1)
        return _ok(s, e, P_MONTH)
    if re.search(r"\bbulan\s+lalu\b", text):
        s, e = _month_range(today.year, today.month, tz, offset=-1)
        return _ok(s, e, P_MONTH)

    # -- 9. weekday name, optional "depan" -----------------------------------
    #       "minggu" here means SUNDAY: a bare "minggu" (no depan/lalu, handled
    #       above) or "hari minggu". "senin depan" etc. are the other weekdays.
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{re.escape(name)}\b", text):
            if name == "minggu" and re.search(r"\bminggu\s+(depan|lalu)\b", text):
                continue  # already handled as week-range above
            is_next_week = bool(re.search(rf"\b{re.escape(name)}\s+depan\b", text))
            if is_next_week:
                if WEEKDAY_DEPAN_IS_NEAREST:
                    primary_d = _upcoming_weekday(today, wd)
                    alt_d = primary_d + timedelta(days=7)
                else:
                    alt_d = _upcoming_weekday(today, wd)
                    primary_d = alt_d + timedelta(days=7)
                s, e = _day_range(primary_d, tz)
                as_, ae = _day_range(alt_d, tz)
                return _ok(s, e, P_DAY, ambiguous=True, alternatives=[(as_, ae)],
                           note=f"{name} depan: bisa juga {alt_d.isoformat()}")
            d = _upcoming_weekday(today, wd)
            s, e = _day_range(d, tz)
            return _ok(s, e, P_DAY, note=f"hari {name} terdekat")

    # -- 10. day words + optional part-of-day --------------------------------
    day_offsets = (
        ("hari ini", 0), ("besok", 1), ("lusa", 2), ("kemarin", -1),
    )
    part = next((p for p in PART_OF_DAY if re.search(rf"\b{p}\b", text)), None)
    for word, offset in day_offsets:
        if re.search(rf"\b{word}\b", text):
            d = today + timedelta(days=offset)
            if part:
                s, e = _part_range(d, part, tz)
                return _ok(s, e, P_PART)
            s, e = _day_range(d, tz)
            return _ok(s, e, P_DAY)

    # -- 11. "nanti <part>" = today's part; bare "<part>" = today's part -----
    if re.search(r"\bnanti\b", text) and part:
        s, e = _part_range(today, part, tz)
        return _ok(s, e, P_PART)
    if part:
        s, e = _part_range(today, part, tz)
        return _ok(s, e, P_PART, note="tanpa hari; diasumsikan hari ini")

    return Resolution(False, raw=text)
