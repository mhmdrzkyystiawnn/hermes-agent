"""Near-term temporal recall — build the context block injected via `prefetch()`.

Design (Fase 1 langkah 4, approved D1-D6):

* NEAR-TERM: items whose [time_start, time_end] INTERSECTS now +/- 7 days, in the
  session timezone. Anchor is the wall clock at prefetch time.
* HISTORICAL: only when the query both names a PAST time reference AND asks for
  information (D6 — a bare "kemarin" in a statement is not a recall request). The
  past expression is resolved by `resolver` to an absolute interval and we query
  items overlapping THAT. If no specific target resolves, fall back to a bounded
  lookback (D3), still capped.
* Output is PLAINTEXT. Core wraps it in `<memory-context>` (build_memory_context_block)
  — we must NOT fence it ourselves.
* Anti-accumulation (D1): the snapshot is capped at MAX_ITEMS items / MAX_CHARS
  characters. This bounds the NEW bytes per turn; the core still replays each turn's
  sidecar, so total replay cost stays O(n^2). Accepted trade-off, no core change.
* Fail-open: any error returns "" so a turn never breaks.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import resolver
import storage

logger = logging.getLogger(__name__)

# --- caps (D2) --------------------------------------------------------------
MAX_ITEMS = 8
MAX_CHARS = 400
NEAR_TERM_DAYS = 7
HISTORICAL_FALLBACK_DAYS = 365
LOW_CONFIDENCE = 0.5

_HEADER = "Temporal memory (absolute dates, tz {tz}):"

# --- historical detection (D6, deterministic — no LLM) ----------------------
# A past time REFERENCE alone is not a recall request; the user must also be ASKING.
_PAST_REF_RE = re.compile(
    r"\b(?:kemarin|kmrn|tadi\s+(?:pagi|siang|sore|malam)|"
    r"(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|\d{1,2})\s+"
    r"(?:hari|minggu|bulan|tahun)\s+(?:yang\s+)?(?:lalu|kemudian)|"
    r"minggu\s+lalu|bulan\s+lalu|tahun\s+lalu|"
    r"\d{1,2}\s*(?:januari|februari|maret|april|mei|juni|juli|agustus|september|"
    r"oktober|november|desember|jan|feb|mar|apr|jun|jul|agu|ags|sep|sept|okt|nov|des)|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"yang\s+lalu|sebelumnya|dulu)\b",
    re.IGNORECASE,
)

# Asking for information / recall. "apa yang terjadi...", "masih ingat...", "kapan...".
_RETRIEVAL_RE = re.compile(
    r"\b(?:apa(?:an)?|kapan|siapa|mana|dimana|di\s+mana|gimana|bagaimana|"
    r"ingat|inget|masih\s+ingat|masih\s+inget|jelas(?:kan)?|cerita(?:in|kan)?|"
    r"kasih\s+tau|kasih\s+tahu|rekap|review|recall|ulang|pernah)\b",
    re.IGNORECASE,
)

# Explicitly asking about a cancelled/superseded event (D5).
_CANCELLED_RE = re.compile(
    r"\b(?:batal|dibatalkan|dibatalin|dibatalkah|cancel(?:led)?|diganti|"
    r"digantikan|superseded|dibuang|tidak\s+jadi|gak\s+jadi|nggak\s+jadi)\b",
    re.IGNORECASE,
)

# Candidate temporal spans to hand to the resolver (longest-first wins).
# Order matters inside the alternation: the fully-specified forms must come BEFORE
# the bare "tanggal N", or the bare form wins and drops the month/year.
_CANDIDATE_RE = re.compile(
    r"\b(?:tanggal\s+\d{1,2}\s*(?:januari|februari|maret|april|mei|juni|juli|agustus|"
    r"september|oktober|november|desember|jan|feb|mar|apr|jun|jul|agu|ags|sep|sept|okt|nov|des)"
    r"(?:\s+\d{4})?|"
    r"\d{1,2}\s*(?:januari|februari|maret|april|mei|juni|juli|agustus|september|"
    r"oktober|november|desember|jan|feb|mar|apr|jun|jul|agu|ags|sep|sept|okt|nov|des)"
    r"(?:\s+\d{4})?|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|\d{1,2})\s+"
    r"(?:hari|minggu|bulan|tahun)\s+(?:yang\s+)?(?:lalu|lagi|kemudian|depan)|"
    r"hari\s+ini|besok|bsk|lusa|kemarin|kmrn|"
    r"tadi\s+(?:pagi|siang|sore|malam|subuh)|"
    r"minggu\s+(?:lalu|depan|ini)|bulan\s+(?:lalu|depan|ini)|tahun\s+(?:lalu|depan|ini)|"
    r"tanggal\s+\d{1,2})\b",
    re.IGNORECASE,
)


def is_historical_request(query: str) -> bool:
    """True when the user ASKS for past information (D6).

    Both a past reference AND a retrieval signal must be present. A statement like
    "kemarin gue rapat" is NOT a historical request.
    """
    text = query or ""
    return bool(_PAST_REF_RE.search(text) and _RETRIEVAL_RE.search(text))


def wants_cancelled(query: str) -> bool:
    """True when the user explicitly asks about cancelled/superseded events (D5)."""
    return bool(_CANCELLED_RE.search(query or ""))


def _candidate_spans(query: str) -> List[str]:
    """Distinct temporal spans in the query, most specific (longest) first."""
    seen: List[str] = []
    for m in _CANDIDATE_RE.finditer(query or ""):
        span = m.group(0).strip()
        if span and span.lower() not in {s.lower() for s in seen}:
            seen.append(span)
    return sorted(seen, key=len, reverse=True)


def resolve_historical_target(query: str, *, tz, now: float):
    """Resolve the query's most specific temporal expression to an absolute interval.

    Returns a matched ``resolver.Resolution`` or None. Deterministic; no LLM.
    """
    anchor = datetime.fromtimestamp(float(now), tz=tz)
    for span in _candidate_spans(query):
        try:
            res = resolver.resolve(span, anchor, tz=tz)
        except Exception as exc:  # resolver is total; belt and braces
            logger.debug("temporal recall: resolve failed for %r: %s", span, exc)
            continue
        if res.matched and res.start is not None and res.end is not None:
            return res
    return None


def _fmt_date(ts: float, tz) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=tz).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def _line_for(item: Dict[str, Any], tz) -> str:
    """One plaintext line: absolute date + precision + summary + kind/epistemic + markers."""
    date = _fmt_date(item.get("time_start"), tz)
    precision = item.get("precision") or ""
    summary = (item.get("summary") or item.get("raw_expression") or "").strip()
    kind = item.get("kind") or ""
    epistemic = item.get("epistemic") or ""
    bits = [f"- {date} ({precision}) {summary}".rstrip(), f"[{kind}]" if kind else ""]
    if epistemic and epistemic != "factual":
        bits.append(f"[{epistemic}]")
    if item.get("ambiguous"):
        bits.append("[ambigu]")
    try:
        if float(item.get("confidence", 1.0)) < LOW_CONFIDENCE:
            bits.append("[?]")
    except (TypeError, ValueError):
        pass
    if item.get("status") in ("cancelled", "superseded"):
        bits.append(f"[{item.get('status')}]")
    return " ".join(b for b in bits if b).rstrip()


def _dedupe(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for it in items:
        key = it.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def build_context(
    conn, *, query: str = "", tz, now: Optional[float] = None,
    max_items: int = MAX_ITEMS, max_chars: int = MAX_CHARS,
) -> str:
    """Build the injected temporal context block, or "" when nothing applies.

    Never raises: this runs on the prefetch path and must not break a turn.
    """
    try:
        if conn is None:
            return ""
        now = float(now if now is not None else time.time())

        # --- NEAR-TERM: +/- 7 days, inclusive, active + not stale ---
        near_start = now - NEAR_TERM_DAYS * 86400
        near_end = now + NEAR_TERM_DAYS * 86400
        if wants_cancelled(query):
            # Explicitly asked about cancelled/superseded: any status, still not stale.
            items = storage.items_overlapping(conn, near_start, near_end, include_stale=False)
        else:
            items = storage.items_in_window(conn, near_start, near_end, only_active=True)

        # --- HISTORICAL (D3): only when the user asks about the past ---
        if is_historical_request(query):
            target = resolve_historical_target(query, tz=tz, now=now)
            if target is not None:
                h_start, h_end = target.start.timestamp(), target.end.timestamp()
            else:
                # No resolvable target -> bounded fallback lookback (D3).
                h_start, h_end = now - HISTORICAL_FALLBACK_DAYS * 86400, now
            if wants_cancelled(query):
                hist = storage.items_overlapping(conn, h_start, h_end, include_stale=False)
            else:
                hist = storage.items_in_window(conn, h_start, h_end, only_active=True)
            items = _dedupe([*items, *hist])

        if not items:
            return ""

        # Deterministic order: time_start then id (items_* already order this way),
        # but merging can interleave — re-sort so output is stable.
        items = sorted(items, key=lambda it: (it.get("time_start") or 0.0, it.get("id") or 0))

        # --- caps (D2): whole lines only, deterministic, header counts too ---
        header = _HEADER.format(tz=str(getattr(tz, "key", tz) or ""))
        lines: List[str] = []
        budget = max_chars - (len(header) + 1)  # header + its newline
        for item in items[:max_items]:
            line = _line_for(item, tz)
            cost = len(line) + (1 if lines else 0)  # newline between lines
            if cost > budget:
                # Would exceed the character cap: stop rather than truncate mid-line.
                break
            lines.append(line)
            budget -= cost
        if not lines:
            return ""
        return f"{header}\n" + "\n".join(lines)
    except Exception as exc:  # fail-open (D11)
        logger.debug("temporal recall: build_context failed (fail-open): %s", exc)
        return ""


def count_items(conn, *, query: str = "", tz, now: Optional[float] = None,
                max_items: int = MAX_ITEMS) -> int:
    """Number of items the last build_context would surface (for recall_status)."""
    try:
        block = build_context(conn, query=query, tz=tz, now=now, max_items=max_items)
        return sum(1 for ln in block.splitlines() if ln.startswith("- ")) if block else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Explicit tool recall (Fase 1 langkah 5): resolve an expression, return items.
# ---------------------------------------------------------------------------


def recall_range(
    conn, *, expression: str, tz, now: Optional[float] = None,
    include_cancelled: bool = False, max_items: int = MAX_ITEMS,
) -> Dict[str, Any]:
    """Resolve ``expression`` and return items overlapping that interval.

    Deterministic; no LLM. Returns ``{"matched", "start", "end", "precision",
    "items": [...]}`` (times as epoch seconds). Never raises.
    """
    out: Dict[str, Any] = {"matched": False, "start": None, "end": None,
                           "precision": "", "items": []}
    try:
        if conn is None:
            return out
        now = float(now if now is not None else time.time())
        anchor = datetime.fromtimestamp(now, tz=tz)
        res = resolver.resolve(expression or "", anchor, tz=tz)
        if not res.matched or res.start is None or res.end is None:
            return out
        out["matched"] = True
        out["start"] = res.start.timestamp()
        out["end"] = res.end.timestamp()
        out["precision"] = res.precision
        start, end = out["start"], out["end"]
        if include_cancelled:
            items = storage.items_overlapping(conn, start, end, include_stale=False)
        else:
            items = storage.items_in_window(conn, start, end, only_active=True)
        items = sorted(items, key=lambda it: (it.get("time_start") or 0.0, it.get("id") or 0))
        out["items"] = [
            {
                "date": _fmt_date(it.get("time_start"), tz),
                "precision": it.get("precision") or "",
                "summary": it.get("summary") or it.get("raw_expression") or "",
                "raw_expression": it.get("raw_expression") or "",
                "kind": it.get("kind") or "",
                "epistemic": it.get("epistemic") or "",
                "status": it.get("status") or "",
                "ambiguous": bool(it.get("ambiguous")),
                "confidence": it.get("confidence"),
                "source_session_id": it.get("source_session_id"),
                "source_message_id": it.get("source_message_id"),
                "source_message_uid": it.get("source_message_uid"),
            }
            for it in items[:max_items]
        ]
        return out
    except Exception as exc:  # fail-open
        logger.debug("temporal recall: recall_range failed (fail-open): %s", exc)
        return out
