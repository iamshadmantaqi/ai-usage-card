"""30-day totals. `Summary` is the only thing the renderer sees, and it holds nothing but model
names, tool names, dates and numbers: no paths, session ids, message ids or project names."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
    TIMEZONE = ZoneInfo("Asia/Dhaka")
except Exception:  # pragma: no cover - a Mac without tzdata; Dhaka has no daylight saving
    TIMEZONE = timezone(timedelta(hours=6))

from .load import UsageRecord
from .pricing import ModelRates, price
from .privacy import looks_private

WINDOW_DAYS = 30
TOP_MODELS = 6
MAX_TOOLS = 4
EXCLUDED_MODELS = frozenset({"<synthetic>"})
TOOL_LABELS = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok", "cursor": "Cursor",
               "opencode": "OpenCode", "antigravity": "Antigravity", "pi": "Pi"}
OTHER_MODELS = "other"
OTHER_TOOLS = "Other tools"
# Names that mean nothing to a reader of the public card. Only the label changes: records keep
# the source name, so pricing and exclusions still see it.
DISPLAY_NAMES = {("cursor", "default"): "cursor-auto"}  # Cursor records its Auto model as "default"
# Only plain model-name characters pass; anything else, or anything that looks like an id, is shown as "other".
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\] ]{0,63}$")


def _safe(label: str) -> bool:
    return bool(_SAFE_LABEL.match(label)) and not looks_private(label)


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
class Summary:
    updated: date
    first_day: date
    last_day: date
    total_tokens: int
    total_cost_usd: float
    daily: List[DayTotal]
    tools: List[Slice]
    models: List[Slice]


def local_day(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, TIMEZONE).date()


def window(today: date) -> List[date]:
    return [today - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]


def window_start_ms(today: date) -> int:
    """Start of the window as T3 reads it: the first day at 00:00 UTC (T3 then adds 36 hours of slack for file mtimes)."""
    first = window(today)[0]
    return int(datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp() * 1000)


def _tool_label(provider: str) -> str:
    label = TOOL_LABELS.get(provider)
    if label:
        return label
    return provider.capitalize() if _safe(provider) else OTHER_TOOLS


def _model_label(record: UsageRecord) -> str:
    label = DISPLAY_NAMES.get((record.provider, record.model), record.model)
    return label if _safe(label) else OTHER_MODELS


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


def summarise(records: Iterable[UsageRecord], rates: Dict[str, ModelRates], today: date,
              newest_ms: Optional[int] = None) -> Summary:
    days = window(today)
    daily = {d: [0, 0.0] for d in days}
    by_tool = {}  # type: Dict[str, List[float]]
    by_model = {}  # type: Dict[str, List[float]]
    newest = newest_ms or 0
    for record in records:
        newest = max(newest, record.timestamp_ms)
        if record.model in EXCLUDED_MODELS:
            continue
        day = local_day(record.timestamp_ms)
        if day not in daily:
            continue
        tokens, cost = record.tokens.total, price(rates, record)
        daily[day][0] += tokens
        daily[day][1] += cost
        for table, label in ((by_tool, _tool_label(record.provider)),
                             (by_model, _model_label(record))):
            row = table.setdefault(label, [0, 0.0])
            row[0] += tokens
            row[1] += cost
    return Summary(
        updated=local_day(newest) if newest else today,
        first_day=days[0],
        last_day=days[-1],
        total_tokens=sum(int(v[0]) for v in daily.values()),
        total_cost_usd=sum(v[1] for v in daily.values()),
        daily=[DayTotal(d, int(daily[d][0]), daily[d][1]) for d in days],
        tools=_top(by_tool, MAX_TOOLS if len(by_tool) <= MAX_TOOLS else MAX_TOOLS - 1, OTHER_TOOLS),
        models=_top(by_model, TOP_MODELS, OTHER_MODELS),
    )
