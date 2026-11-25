"""One-off repair: put Cursor's usage back into days the history froze without it.

The history never recomputes a frozen day, so a day frozen while Cursor's usage was missing (an account T3 Code had
not read yet, or Cursor switched off) keeps that gap. `repair_cursor` rebuilds the `cursor` rows of every frozen day
that T3 Code's Cursor cache still holds, with the same loader, dedupe, rows and pricing the card freezes with, and
leaves every other tool's rows alone. A day the cache does not hold is not touched. The whole repair refuses when a
day it would change is not fully covered by every account in the cache (it starts before the latest `sinceMs` or
ends after the earliest `untilMs`, a lapsed account's included, since its usage after that is unknown), or when a
day would end up with fewer Cursor tokens than the history holds (the repair only puts missing usage back). Running
it again changes nothing.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import history as ledger_file
from .history import Ledger, LedgerError, Row
from .load import CacheError, cursor_cache_path, load_cursor_coverage, load_t3
from .pricing import load_rates
from .summarise import TIMEZONE, live_days

TOOL = "cursor"


@dataclass(frozen=True)
class Change:
    day: date
    before: Tuple[int, float]  # Cursor tokens and cost the history holds
    after: Tuple[int, float]  # and what the cache gives


COST_TOLERANCE = 1e-5  # frozen costs differ from a recomputed one by float noise of about 3e-6 a row; that is not a gap


def _same(a: List[Row], b: List[Row]) -> bool:
    """The same models and tokens, and costs equal to within float noise."""
    key = lambda rows: sorted(rows, key=lambda r: (r.model, r.tokens))
    a, b = key(a), key(b)
    return len(a) == len(b) and all((x.model, x.tokens) == (y.model, y.tokens) and abs(x.cost_usd - y.cost_usd) <= COST_TOLERANCE
                                    for x, y in zip(a, b))


def _sum(rows: List[Row]) -> Tuple[int, float]:
    return sum(r.tokens for r in rows), sum(r.cost_usd for r in rows)


def _midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TIMEZONE).timestamp() * 1000)


def repair_cursor(ledger: Ledger, userdata: Path) -> Tuple[Ledger, List[Change]]:
    """The repaired history and the days that change. Raises CacheError or LedgerError when it cannot be trusted."""
    account_records, since, until = load_cursor_coverage(cursor_cache_path(userdata))
    if not account_records or since is None or until is None:
        raise CacheError("no Cursor usage in the cache: nothing to repair from")
    # The records a freeze reads: the scan cache and the Cursor account cache, de-duplicated together.
    records = [r for r in load_t3(userdata)[0] if r.provider == TOOL]
    fresh = {day: [r for r in rows if r.tool == TOOL] for day, rows in live_days(records, load_rates(userdata)).items()}
    if ledger.start is None or not ledger.days:
        raise LedgerError("the history has no frozen days")
    last = max(ledger.days)
    days, changes = dict(ledger.days), []  # type: Dict[date, List[Row]], List[Change]
    for day, cursor_rows in sorted(fresh.items()):
        if not (ledger.start <= day <= last) or not cursor_rows:
            continue  # not frozen, or no Cursor usage that day
        kept = [r for r in ledger.days.get(day, []) if r.tool != TOOL]
        current = [r for r in ledger.days.get(day, []) if r.tool == TOOL]
        if _same(cursor_rows, current):
            continue
        if _midnight_ms(day) < since or _midnight_ms(day + timedelta(days=1)) > until:
            raise CacheError("the Cursor cache does not cover %s in full (it covers %s to %s): not repaired"
                             % (day, _stamp(since), _stamp(until)))
        if _sum(cursor_rows)[0] < _sum(current)[0]:
            raise CacheError("the Cursor cache gives %s fewer tokens (%d) than the history holds (%d): not repaired"
                             % (day, _sum(cursor_rows)[0], _sum(current)[0]))
        days[day] = kept + cursor_rows
        changes.append(Change(day, _sum(current), _sum(cursor_rows)))
    return Ledger(days, ledger.start), changes


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, TIMEZONE).strftime("%Y-%m-%d %H:%M")


def table(changes: List[Change]) -> str:
    """Contiguous changed days as ranges: Cursor tokens and cost before, after and added."""
    ranges = []  # type: List[List[Change]]
    for change in changes:
        if ranges and change.day - ranges[-1][-1].day == timedelta(days=1):
            ranges[-1].append(change)
        else:
            ranges.append([change])
    lines = ["%-23s %5s %15s %11s %15s %11s %15s %11s" % ("range", "days", "tokens before", "$ before", "tokens after", "$ after", "tokens added", "$ added")]
    totals = [0, 0.0, 0, 0.0]
    for group in ranges:
        sums = [sum(c.before[0] for c in group), sum(c.before[1] for c in group),
                sum(c.after[0] for c in group), sum(c.after[1] for c in group)]
        totals = [a + b for a, b in zip(totals, sums)]
        label = str(group[0].day) if len(group) == 1 else "%s to %s" % (group[0].day, group[-1].day)
        lines.append("%-23s %5d %15d %11.2f %15d %11.2f %15d %11.2f" % (label, len(group), sums[0], sums[1], sums[2], sums[3], sums[2] - sums[0], sums[3] - sums[1]))
    lines.append("%-23s %5d %15d %11.2f %15d %11.2f %15d %11.2f" % ("total", len(changes), totals[0], totals[1], totals[2], totals[3], totals[2] - totals[0], totals[3] - totals[1]))
    return "\n".join(lines)


def run(history_path: Path, userdata: Path, dry_run: bool, now: Optional[datetime] = None) -> Tuple[str, Optional[Path]]:
    """The table, and the backup file written (None for a dry run or when nothing changes). Writes only
    after a timestamped copy of the history sits next to it."""
    saved = ledger_file.load(history_path)
    repaired, changes = repair_cursor(saved, userdata)
    if not changes:
        return "nothing to repair: the history already holds the Cursor usage the cache gives", None
    text = table(changes)
    if dry_run:
        return text + "\n(dry run: nothing written)", None
    backup = history_path.with_name("%s.%s.bak" % (history_path.name, (now or datetime.now(TIMEZONE)).strftime("%Y%m%dT%H%M%S")))
    shutil.copy2(str(history_path), str(backup))
    ledger_file.save(history_path, repaired)
    return text + "\nrepaired %d days; backup %s" % (len(changes), backup), backup
