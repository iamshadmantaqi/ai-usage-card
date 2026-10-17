"""API-equivalent pricing, mirroring T3 Code's `usagePricing.ts` so the card matches its usage view.

Rates come from T3's cached LiteLLM table (`usage-model-rates.json`, `{fetchedAtMs, document}`).
Cost is list price at the API, not what was paid.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .load import CacheError, UsageRecord, _read_json, _num

RATES_FILE = "usage-model-rates.json"
# Never priced, whatever the table says: locally generated messages, and bare family names that
# are ambiguous across generations.
UNPRICEABLE = frozenset({"<synthetic>", "synthetic", "opus", "sonnet", "haiku", "fable"})


@dataclass(frozen=True)
class TokenRates:
    input: float
    output: float
    cache_read: float
    cache_creation: float
    cache_creation_1h: float  # LiteLLM's `cache_creation_input_token_cost_above_1hr`, else the 5-minute rate


@dataclass(frozen=True)
class ModelRates:
    standard: TokenRates
    fast: Optional[TokenRates]
    ultrafast: Optional[TokenRates]

    def at(self, speed: str) -> TokenRates:
        chosen = None if speed == "standard" else (self.fast if speed == "fast" else self.ultrafast)
        return chosen or self.standard


def _read_rates(entry: Dict[str, Any], suffix: str, standard: Optional[TokenRates]) -> Optional[TokenRates]:
    """One tier of rates. Cache rates that the table omits fall back to the input rate (or the
    standard tier's cache-to-input ratio), never to free."""
    inp, out = entry.get("input_cost_per_token" + suffix), entry.get("output_cost_per_token" + suffix)
    if not _num(inp) or not _num(out):
        return None

    def cache(name: str, field: str) -> float:
        value = entry.get(name + suffix)
        if _num(value):
            return float(value)
        if standard is not None and standard.input > 0:
            return getattr(standard, field) / standard.input * inp
        return float(inp)

    creation = cache("cache_creation_input_token_cost", "cache_creation")
    one_hour = entry.get("cache_creation_input_token_cost_above_1hr" + suffix)
    return TokenRates(float(inp), float(out), cache("cache_read_input_token_cost", "cache_read"), creation,
                      float(one_hour) if _num(one_hour) else creation)


def _scale(rates: TokenRates, multiple: float) -> TokenRates:
    return TokenRates(rates.input * multiple, rates.output * multiple, rates.cache_read * multiple,
                      rates.cache_creation * multiple, rates.cache_creation_1h * multiple)


def _fast_multiplier(entry: Dict[str, Any]) -> Optional[float]:
    specific = entry.get("provider_specific_entry")
    fast = specific.get("fast") if isinstance(specific, dict) else None
    return float(fast) if _num(fast) and fast > 0 else None


def _normalise(model: str) -> str:
    return model.strip().lower()


def _bare(key: str) -> str:
    return key[key.rfind("/") + 1:]


def parse_rate_table(document: Any) -> Dict[str, ModelRates]:
    table = {}  # type: Dict[str, ModelRates]
    if not isinstance(document, dict):
        return table
    for name, entry in document.items():
        if not isinstance(entry, dict):
            continue
        standard = _read_rates(entry, "", None)
        key = _normalise(name)
        if standard is None or not key:
            continue
        multiple = _fast_multiplier(entry)
        table[key] = ModelRates(standard,
                                _scale(standard, multiple) if multiple is not None else _read_rates(entry, "_priority", standard),
                                _read_rates(entry, "_ultrafast", standard))
    # A bare name ("gpt-6-luna") is aliased only when no canonical entry exists and every
    # qualified entry ("openai/gpt-6-luna") has the same rate.
    candidates = {}  # type: Dict[str, Optional[ModelRates]]
    for key, rate in table.items():
        alias = _bare(key)
        if not alias or alias == key or alias in table:
            continue
        if alias not in candidates:
            candidates[alias] = rate
        elif candidates[alias] is not None and candidates[alias] != rate:
            candidates[alias] = None
    for alias, rate in candidates.items():
        if rate is not None:
            table[alias] = rate
    return table


def load_rates(userdata_dir: Path) -> Dict[str, ModelRates]:
    document = _read_json(userdata_dir / RATES_FILE)
    table = parse_rate_table(document.get("document") if isinstance(document, dict) else None)
    if not table:
        raise CacheError("rate table is empty or unreadable")
    return table


def lookup_rate(table: Dict[str, ModelRates], model: str) -> Optional[ModelRates]:
    key = _normalise(model)
    bracket = key.find("[")
    key = key if bracket == -1 else key[:bracket]  # "claude-fable-5-1[1m]" is priced at its base tier
    if not _bare(key) or _bare(key) in UNPRICEABLE:
        return None
    return table.get(key)


def price(table: Dict[str, ModelRates], record: UsageRecord) -> float:
    """Dollar cost of one record. A provider-reported cost replaces the computed one; an unpriced
    model with no reported cost counts as $0. Reasoning tokens are already inside output tokens."""
    reported = record.reported_cost_usd
    rate = lookup_rate(table, record.rate_model if record.rate_model is not None else record.model)  # T3: rateModel ?? model
    if rate is None:
        return reported if reported is not None else 0.0
    r = rate.at(record.speed)
    t = record.tokens
    one_hour = min(t.cache_creation_1h, t.cache_creation)
    list_cost = (t.uncached * r.input + t.cached * r.cache_read + (t.cache_creation - one_hour) * r.cache_creation
                 + one_hour * r.cache_creation_1h + t.output * r.output)
    if reported is not None and list_cost <= 0:
        return reported
    return reported if reported is not None else list_cost
