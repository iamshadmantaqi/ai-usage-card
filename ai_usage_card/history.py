"""The local history: one frozen record per day, kept so old days outlive the transcripts.

Claude Code deletes its transcripts after 60 days and T3 Code's cache follows them, so a window
longer than that can only be built from days saved earlier. The ledger is a JSON file outside the
repo and outside the published branch::

    {"version": 1, "days": {"2026-08-28": [{"tool": "claude", "model": "claude-opus-5-5", "tokens": 123, "cost_usd": 4.5}]}}

Days are Asia/Dhaka dates. A row holds a tool name, a model name and two numbers, nothing else; both
names pass the same filter as the card (`privacy.safe_label`), and a file that holds anything else is
refused, not repaired. A missing file is an empty ledger. A file that cannot be read or does not
match the layout raises `LedgerError`, and nothing writes over it.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from .privacy import safe_label

VERSION = 1


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

    @staticmethod
    def empty() -> "Ledger":
        return Ledger({})


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _row(day: str, item: Any) -> Row:
    if not isinstance(item, dict) or set(item) != {"tool", "model", "tokens", "cost_usd"}:
        raise LedgerError("history row for %s is not {tool, model, tokens, cost_usd}" % day)
    tool, model = item["tool"], item["model"]
    if not (isinstance(tool, str) and isinstance(model, str) and safe_label(tool) and safe_label(model)):
        raise LedgerError("history row for %s has a tool or model name that is not a plain name" % day)
    if not (_int(item["tokens"]) and _number(item["cost_usd"])):
        raise LedgerError("history row for %s has a tokens or cost that is not a non-negative number" % day)
    return Row(tool, model, item["tokens"], float(item["cost_usd"]))


def parse(text: str) -> Ledger:
    try:
        document = json.loads(text)
    except ValueError as error:
        raise LedgerError("history file is not valid JSON: %s" % error)
    if not isinstance(document, dict) or set(document) != {"version", "days"}:
        raise LedgerError("history file is not {version, days}")
    if document["version"] != VERSION or isinstance(document["version"], bool):
        raise LedgerError("history file has version %r, this code reads version %d" % (document["version"], VERSION))
    if not isinstance(document["days"], dict):
        raise LedgerError("history file 'days' is not an object")
    days = {}  # type: Dict[date, List[Row]]
    for key, items in document["days"].items():
        try:
            day = date.fromisoformat(key)
        except ValueError:
            raise LedgerError("history day %r is not a YYYY-MM-DD date" % key)
        if day.isoformat() != key:
            raise LedgerError("history day %r is not a YYYY-MM-DD date" % key)
        if not isinstance(items, list):
            raise LedgerError("history day %s is not a list of rows" % key)
        days[day] = [_row(key, item) for item in items]
    return Ledger(days)


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
    return json.dumps({"version": VERSION, "days": days}, indent=1, sort_keys=False) + "\n"


def save(path: Path, ledger: Ledger) -> None:
    """Write to a temporary file in the same folder, then rename it over `path`: a crash leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(dump(ledger))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, str(path))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
