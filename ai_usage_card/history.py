"""The local history: one frozen record per day, kept so old days outlive the transcripts.

Claude Code deletes its transcripts after 60 days and T3 Code's cache follows them, so a window
longer than that can only be built from days saved earlier. The ledger is a JSON file outside the
repo and outside the published branch::

    {"version": 1, "start": "2026-08-30", "days": {"2026-08-30": [{"tool": "claude", "model": "claude-opus-5-5", "tokens": 123, "cost_usd": 4.5}]}}

Days are Asia/Dhaka dates. `start` is the first full day, fixed by the first run that freezes anything (a day with
no usage has no entry, so the days alone cannot hold it); a file without it is read as it was written. A row holds a tool name, a model name and two numbers, nothing else; both
names pass the same filter as the card (`privacy.safe_label`), and a file that holds anything else is
refused, not repaired. A missing file is an empty ledger. A file that cannot be read or does not
match the layout raises `LedgerError`, and nothing writes over it.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .privacy import safe_label

VERSION = 1
MAX_NUMBER = 2 ** 53 - 1  # the largest integer a float and JSON readers hold exactly


class LedgerError(Exception):
    """The history file is unreadable or not in the layout this code writes."""


@dataclass(frozen=True)
class Row:
    tool: str
    model: str
    tokens: int
    cost_usd: float


@dataclass(frozen=True)
class Ledger:
    days: Dict[date, List[Row]]
    start: Optional[date] = None

    @staticmethod
    def empty() -> "Ledger":
        return Ledger({})


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_NUMBER


def _number(value: Any) -> bool:
    # Compared before any conversion: NaN fails both tests, infinity and a huge integer fail the upper one.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_NUMBER


def _row(day: str, item: Any) -> Row:
    if not isinstance(item, dict) or set(item) != {"tool", "model", "tokens", "cost_usd"}:
        raise LedgerError("history row for %s is not {tool, model, tokens, cost_usd}" % day)
    tool, model = item["tool"], item["model"]
    if not (isinstance(tool, str) and isinstance(model, str) and safe_label(tool) and safe_label(model)):
        raise LedgerError("history row for %s has a tool or model name that is not a plain name" % day)
    if not (_int(item["tokens"]) and _number(item["cost_usd"])):
        raise LedgerError("history row for %s has a tokens or cost that is not a non-negative number" % day)
    return Row(tool, model, item["tokens"], float(item["cost_usd"]))


def _date(key: Any, what: str) -> date:
    try:
        day = date.fromisoformat(key)
    except (TypeError, ValueError):
        raise LedgerError("history %s %r is not a YYYY-MM-DD date" % (what, key))
    if day.isoformat() != key:
        raise LedgerError("history %s %r is not a YYYY-MM-DD date" % (what, key))
    return day


def parse(text: str) -> Ledger:
    try:
        document = json.loads(text)
    except ValueError as error:
        raise LedgerError("history file is not valid JSON: %s" % error)
    if not isinstance(document, dict) or set(document) not in ({"version", "days"}, {"version", "start", "days"}):
        raise LedgerError("history file is not {version, start, days}")
    if document["version"] != VERSION or isinstance(document["version"], bool):
        raise LedgerError("history file has version %r, this code reads version %d" % (document["version"], VERSION))
    if not isinstance(document["days"], dict):
        raise LedgerError("history file 'days' is not an object")
    start = _date(document["start"], "start") if "start" in document else None
    days = {}  # type: Dict[date, List[Row]]
    for key, items in document["days"].items():
        day = _date(key, "day")
        if start is not None and day < start:
            raise LedgerError("history day %s is before its start %s" % (key, start))
        if not isinstance(items, list):
            raise LedgerError("history day %s is not a list of rows" % key)
        days[day] = [_row(key, item) for item in items]
    return Ledger(days, start)


def load(path: Path) -> Ledger:
    """The saved history; an empty one when the file does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Ledger.empty()
    except (OSError, UnicodeDecodeError) as error:
        raise LedgerError("history file %s cannot be read (%s)" % (path, type(error).__name__))
    return parse(text)


def dump(ledger: Ledger) -> str:
    days = {day.isoformat(): [{"tool": r.tool, "model": r.model, "tokens": r.tokens, "cost_usd": round(r.cost_usd, 8)}
                              for r in sorted(rows, key=lambda r: (r.tool, r.model))]
            for day, rows in sorted(ledger.days.items())}
    document = {"version": VERSION}  # type: Dict[str, Any]
    if ledger.start is not None:
        document["start"] = ledger.start.isoformat()
    document["days"] = days
    try:
        return json.dumps(document, indent=1, sort_keys=False, allow_nan=False) + "\n"
    except ValueError as error:  # a NaN or an infinity
        raise LedgerError("history not written: %s" % error)


def save(path: Path, ledger: Ledger) -> None:
    """Write to a temporary file in the same folder, then rename it over `path`: a crash leaves the old file whole.
    The text is read back with `parse` first, so a file this code would refuse on the next run is never written."""
    text = dump(ledger)
    parse(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, str(path))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
