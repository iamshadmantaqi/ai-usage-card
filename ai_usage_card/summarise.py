"""Totals over a rolling six-month window. `Summary` is the only thing the renderer sees, and it holds
nothing but model names, tool names, dates and numbers: no paths, session ids, message ids or project names.

A day comes from exactly one source. Days at least `FREEZE_AFTER_DAYS` old are read from the saved
history (`history.py`) when it has them; every other day comes from the live records (Claude Code's
transcripts and T3 Code's caches). `freeze` copies live days into the history once they are old enough.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
    TIMEZONE = ZoneInfo("Asia/Dhaka")
except Exception:  # pragma: no cover - a Mac without tzdata; Dhaka has no daylight saving
    TIMEZONE = timezone(timedelta(hours=6))

from .history import Ledger, Row
from .load import UsageRecord
from .pricing import ModelRates, price
from .privacy import safe_label

WINDOW_DAYS = 182  # six months, rolling, ending today
FREEZE_AFTER_DAYS = 30  # a day this old or older is saved to the history and read from it from then on
PARTIAL_AFTER_HOUR = 2  # the oldest day of the first run is cut off if its first record is at or after 02:00 Dhaka
TOP_MODELS = 6
MAX_TOOLS = 4
# Providers whose usage comes from their own servers (Cursor's account API), not from files that a cleanup deletes
# after 60 days. They reach back further than the others, so they do not decide where the history starts.
LONG_HISTORY_PROVIDERS = frozenset({"cursor"})
EXCLUDED_MODELS = frozenset({"<synthetic>"})
TOOL_LABELS = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok", "cursor": "Cursor",
               "opencode": "OpenCode", "antigravity": "Antigravity", "pi": "Pi"}
OTHER_KEY = "other"
OTHER_MODELS = "other"
OTHER_TOOLS = "Other tools"
# Names that mean nothing to a reader of the public card. Only the label changes: records keep
# the source name, so pricing and exclusions still see it.
DISPLAY_NAMES = {("cursor", "default"): "cursor-auto"}  # Cursor records its Auto model as "default"


@dataclass(frozen=True)
class DayTotal:
    day: date
    tokens: int
    cost_usd: float


@dataclass(frozen=True)
class Slice:
    """A tool or a model: a label, tokens and API-equivalent cost."""
    label: str
    tokens: int
    cost_usd: float


@dataclass(frozen=True)
class WeekTotal:
    """One bar of the chart: a week starting Monday (Dhaka), clipped to the window. `current` is the
    week that holds today, which is still being written."""
    first: date
    last: date
    tokens: int
    cost_usd: float
    current: bool


@dataclass(frozen=True)
class Summary:
    updated: date
    first_day: date  # the first day counted: the first full day of history, or the window's first day once it is full
    last_day: date
    full_window: bool  # True when the history reaches back the whole window ("LAST 6 MONTHS")
    total_tokens: int
    total_cost_usd: float
    daily: List[DayTotal]
    weekly: List[WeekTotal]
    tools: List[Slice]
    models: List[Slice]


def local_dt(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, TIMEZONE)


def local_day(timestamp_ms: int) -> date:
    return local_dt(timestamp_ms).date()


def window_first(today: date) -> date:
    return today - timedelta(days=WINDOW_DAYS - 1)


def window_start_ms(today: date) -> int:
    """Start of the window as T3 reads it: the first day at 00:00 UTC (T3 then adds 36 hours of slack for file mtimes)."""
    first = window_first(today)
    return int(datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp() * 1000)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())  # Monday


def _tool_key(provider: str) -> str:
    return provider if safe_label(provider) else OTHER_KEY


def _tool_label(key: str) -> str:
    label = TOOL_LABELS.get(key)
    if label:
        return label
    return OTHER_TOOLS if key == OTHER_KEY else key.capitalize()


def _model_label(record: UsageRecord) -> str:
    label = DISPLAY_NAMES.get((record.provider, record.model), record.model)
    return label if safe_label(label) else OTHER_MODELS


def _top(totals: Dict[str, List[float]], limit: int, other_label: str) -> List[Slice]:
    """The biggest `limit` rows by tokens; the rest, and anything already labelled `other_label`
    (unsafe names), are folded into one `other_label` row at the end."""
    named = {name: v for name, v in totals.items() if name != other_label}
    ranked = sorted(named.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    kept = [Slice(name, int(v[0]), v[1]) for name, v in ranked[:limit]]
    rest = ranked[limit:] + ([(other_label, totals[other_label])] if other_label in totals else [])
    if rest:
        kept.append(Slice(other_label, int(sum(v[0] for _, v in rest)), sum(v[1] for _, v in rest)))
    return kept


def live_days(records: Iterable[UsageRecord], rates: Dict[str, ModelRates]) -> Dict[date, List[Row]]:
    """The live records as one row per day, tool and model, priced, with names already filtered. These are the rows the history stores."""
    table = {}  # type: Dict[date, Dict[tuple, List[float]]]
    for record in records:
        if record.model in EXCLUDED_MODELS:
            continue
        cell = table.setdefault(local_day(record.timestamp_ms), {}).setdefault(
            (_tool_key(record.provider), _model_label(record)), [0, 0.0])
        cell[0] += record.tokens.total
        cell[1] += price(rates, record)
    return {day: [Row(tool, model, int(v[0]), v[1]) for (tool, model), v in sorted(cells.items())]
            for day, cells in table.items()}


def _frozen(ledger: Ledger, today: date) -> Dict[date, List[Row]]:
    """The history days that are read from the history: old enough, and not in the future. A younger day is always live."""
    return {day: rows for day, rows in ledger.days.items() if timedelta(days=FREEZE_AFTER_DAYS) <= today - day}


def _anchor(records: Iterable[UsageRecord]) -> List[UsageRecord]:
    """The records that decide where the history starts: the file-based tools, which the 60-day cleanup cuts. With
    only long-history tools in the data, those are used."""
    usable = [r for r in records if r.model not in EXCLUDED_MODELS]
    return [r for r in usable if r.provider not in LONG_HISTORY_PROVIDERS] or usable


def cut_off_day(records: Iterable[UsageRecord], ledger: Ledger, today: date) -> Optional[date]:
    """The oldest live day when it is cut off part way, else None.

    Claude Code deletes a transcript 60 days after its last write, so the oldest day it still holds
    keeps only the files written after the cut. That day is told by its first record: at or after
    02:00 Dhaka means the morning is missing. The test only applies to the first run, when the history
    has no day on or before it, and only to a day old enough to be frozen: a younger oldest day is a
    real start of use. A person who really starts after 02:00 loses that one day, which is harmless."""
    stamps = [r.timestamp_ms for r in _anchor(records)]
    if not stamps:
        return None
    first = local_dt(min(stamps))
    day = first.date()
    if today - day < timedelta(days=FREEZE_AFTER_DAYS) or any(known <= day for known in ledger.days):
        return None
    return day if first.hour >= PARTIAL_AFTER_HOUR else None


def history_start(records: Iterable[UsageRecord], ledger: Ledger, today: date) -> date:
    """The first full day: the day after a cut-off one, else the first day any saved or live data has, else today."""
    records = list(records)
    skip = cut_off_day(records, ledger, today)
    saved = list(_frozen(ledger, today))
    live = [local_day(r.timestamp_ms) for r in _anchor(records)]
    candidates = saved + ([skip + timedelta(days=1)] if skip else [min(live)] if live else [])
    return min(candidates) if candidates else today


def freeze(records: Iterable[UsageRecord], rates: Dict[str, ModelRates], today: date, ledger: Ledger,
           through: Optional[date] = None) -> Ledger:
    """The history with every live day old enough added once. A saved day is never changed. `through`
    is the last day whose data is complete in every source (T3 Code's cache is saved only when its
    usage page is open, so days after its newest record may be missing for the tools it reads); None freezes nothing.
    Days before the history's first full day are left out."""
    records = list(records)
    if through is None:
        return ledger
    start = history_start(records, ledger, today)
    newest = min(today - timedelta(days=FREEZE_AFTER_DAYS), through)
    added = {day: rows for day, rows in live_days(records, rates).items()
             if start <= day <= newest and day not in ledger.days}
    return Ledger({**ledger.days, **added}) if added else ledger


def summarise(records: Iterable[UsageRecord], rates: Dict[str, ModelRates], today: date,
              newest_ms: Optional[int] = None, ledger: Optional[Ledger] = None) -> Summary:
    ledger = ledger or Ledger.empty()
    records = list(records)
    newest = max([newest_ms or 0] + [r.timestamp_ms for r in records])
    saved = _frozen(ledger, today)
    earliest = history_start(records, ledger, today)
    live = {day: rows for day, rows in live_days(records, rates).items() if day not in saved and day >= earliest}
    first = max(earliest, window_first(today))
    days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    by_day = {day: saved.get(day) or live.get(day) or [] for day in days}
    by_tool = {}  # type: Dict[str, List[float]]
    by_model = {}  # type: Dict[str, List[float]]
    daily = []
    for day in days:
        tokens, cost = 0, 0.0
        for row in by_day[day]:
            tokens += row.tokens
            cost += row.cost_usd
            for table, label in ((by_tool, _tool_label(row.tool)), (by_model, row.model)):
                cell = table.setdefault(label, [0, 0.0])
                cell[0] += row.tokens
                cell[1] += row.cost_usd
        daily.append(DayTotal(day, tokens, cost))
    weekly = []
    this_week = week_start(today)
    for total in daily:
        monday = week_start(total.day)
        if weekly and weekly[-1][0] == monday:
            bar = weekly[-1][1]
            bar[1], bar[2], bar[3] = total.day, bar[2] + total.tokens, bar[3] + total.cost_usd
        else:
            weekly.append((monday, [total.day, total.day, total.tokens, total.cost_usd]))
    return Summary(
        updated=local_day(newest) if newest else today,
        first_day=first,
        last_day=today,
        full_window=earliest <= window_first(today),
        total_tokens=sum(d.tokens for d in daily),
        total_cost_usd=sum(d.cost_usd for d in daily),
        daily=daily,
        weekly=[WeekTotal(v[0], v[1], v[2], v[3], monday == this_week) for monday, v in weekly],
        tools=_top(by_tool, MAX_TOOLS if len(by_tool) <= MAX_TOOLS else MAX_TOOLS - 1, OTHER_TOOLS),
        models=_top(by_model, TOP_MODELS, OTHER_MODELS),
    )
