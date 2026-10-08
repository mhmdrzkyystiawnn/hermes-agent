"""CLI for the temporal memory provider — ``hermes temporal list|delete``.

Discovered by ``plugins.memory.discover_plugin_cli_commands`` (active provider only),
which imports THIS module (``register_cli(subparser)``) and wires the handler named
``<provider>_command`` as ``args.func``. It never imports the provider module, so this
file must be self-contained: it opens the DB through ``storage.open_db_for_home`` at an
EXPLICIT home (``get_hermes_home()``), never the process-global one.

Scope (MVP): list, delete. No re-extract, no edit.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional

# This module is loaded by plugins.memory.discover_plugin_cli_commands via
# spec_from_file_location, NOT as part of the provider package, so sibling imports
# (storage/resolver) are not on sys.path by default. Put this file's own directory
# there first — the same directory the provider package lives in.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage  # noqa: E402

TZ_NAME = "Asia/Jakarta"


def _tz():
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:
        from datetime import timezone
        return timezone.utc


def _fmt_ts(ts, tz) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def _open():
    from hermes_constants import get_hermes_home

    conn = storage.open_db_for_home(get_hermes_home())
    storage.init_schema(conn)
    return conn


def _render_row(item, tz) -> str:
    """One line: id + absolute range + summary + markers. Status/stale always visible."""
    start = _fmt_ts(item.get("time_start"), tz)
    end = _fmt_ts(item.get("time_end"), tz)
    rng = start if start == end else f"{start}..{end}"
    parts = [f"[{item.get('id')}]", rng, f"({item.get('precision') or '?'})",
             (item.get("summary") or item.get("raw_expression") or "").strip()]
    tags = [item.get("kind") or "", item.get("status") or "", item.get("epistemic") or ""]
    parts.append(" ".join(f"{t}" for t in tags if t and t != "factual"))
    if item.get("ambiguous"):
        parts.append("[ambigu]")
    if item.get("stale"):
        parts.append(f"[STALE:{item.get('stale_reason') or 'yes'}]")
    return "  ".join(p for p in parts if p)


def _parse_range(value: Optional[str], tz):
    """``YYYY-MM-DD`` (or ``YYYY-MM-DD..YYYY-MM-DD``) -> (start_epoch, end_epoch)."""
    if not value:
        return None, None
    text = value.strip()
    if ".." in text:
        a, b = text.split("..", 1)
    else:
        a = b = text
    try:
        d1 = datetime.strptime(a.strip(), "%Y-%m-%d").replace(tzinfo=tz)
        d2 = datetime.strptime(b.strip(), "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        return None, None
    start = d1.replace(hour=0, minute=0, second=0, microsecond=0)
    end = d2.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start.timestamp(), end.timestamp()


def _cmd_list(args) -> int:
    tz = _tz()
    conn = _open()
    try:
        start, end = _parse_range(getattr(args, "range", None), tz)
        if getattr(args, "range", None) and start is None:
            print("error: --range must be YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD", file=sys.stderr)
            return 2
        items = storage.list_items(
            conn, start=start, end=end,
            status=getattr(args, "status", None),
            include_stale=not getattr(args, "no_stale", False),
            limit=getattr(args, "limit", None),
        )
        if not items:
            print("(no temporal items)")
            return 0
        for it in items:
            print(_render_row(it, tz))
        print(f"\n{len(items)} item(s)")
        return 0
    finally:
        conn.close()


def _cmd_delete(args) -> int:
    conn = _open()
    try:
        try:
            item_id = int(args.id)
        except (TypeError, ValueError):
            print(f"error: id must be an integer, got {args.id!r}", file=sys.stderr)
            return 2
        existed = storage.delete_item(conn, item_id)
        if not existed:
            print(f"error: no temporal item with id {item_id}", file=sys.stderr)
            return 1
        print(f"deleted temporal item {item_id}")
        return 0
    finally:
        conn.close()


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes temporal`` argparse tree."""
    subs = subparser.add_subparsers(dest="temporal_command")

    p_list = subs.add_parser("list", help="List stored temporal items")
    p_list.add_argument("--range", default=None, metavar="D[..D]",
                        help="Filter by day or day range (YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD)")
    p_list.add_argument("--status", default=None, choices=["active", "cancelled", "superseded"],
                        help="Filter by status (default: all)")
    p_list.add_argument("--no-stale", action="store_true",
                        help="Hide stale items (default: stale shown, tagged)")
    p_list.add_argument("--limit", type=int, default=None, help="Max rows")
    p_list.set_defaults(func=_cmd_list)

    p_del = subs.add_parser("delete", help="Delete one temporal item by id")
    p_del.add_argument("id", help="Item id (from `hermes temporal list`)")
    p_del.set_defaults(func=_cmd_delete)

    subparser.set_defaults(func=lambda a: (subparser.print_help(), 0)[1])


def temporal_command(args, **kwargs) -> int:
    """Handler resolved by name as ``<provider>_command``."""
    func = getattr(args, "func", None)
    if func is None:
        return 0
    return int(func(args) or 0)
