"""Read T3 Code's usage caches into typed records.

The record layouts below were read from T3 Code's own source (the bundled server,
`usageScanCache.ts` and `accountUsage.ts` in `provider-cursor`), not guessed.

Scan cache `usage-scan-cache-v5.json`::

    {sources, version: 5, models: [name], sessions: [id], files: {path: entry}}
    entry  = {s: size, m: mtimeMs, p: provider, r: [row], t: [row], o, gl, gh, cs}
    row    = [timestampMs, modelIdx, sessionIdx, uncachedInput, cachedInput,
              cacheCreation, output, reasoning, dedupeKey, reportedCostUsd,
              speedIdx, rateModelIdx]          (speedIdx: 0 standard, 1 fast, 2 ultrafast)

Cursor cache `usage-cursor-account-cache-v1.json`::

    {version: 1, strings: [str], accounts: {key: {accountKey, sinceMs, untilMs,
     fetchedAtMs, records: [row]}}}
    row    = [timestampMs, modelStrIdx, sessionStrIdx, uncachedInput, cachedInput,
              cacheCreation, output, reportedCostUsd, dedupeKey]

Reasoning tokens are a subset of output tokens, so they are never added to a total.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCAN_CACHE_FILE = "usage-scan-cache-v5.json"
CURSOR_CACHE_FILE = "usage-cursor-account-cache-v1.json"
SETTINGS_FILE = "settings.json"
SCAN_CACHE_VERSION = 5
CURSOR_CACHE_VERSION = 1
SPEEDS = ("standard", "fast", "ultrafast")
# T3 re-keys records of these providers when one session spans several files.
SHARED_SESSION_PROVIDERS = frozenset({"codex"})


class CacheError(Exception):
    """The cache is missing, unparsable or in a layout this code does not know."""


@dataclass(frozen=True)
class Tokens:
    uncached: int
    cached: int
    cache_creation: int
    output: int
    reasoning: int
    # Part of `cache_creation` that was written with the 1-hour lifetime (priced higher than the
    # 5-minute default). T3's caches do not split writes, so there it is always 0.
    cache_creation_1h: int = 0

    @property
    def total(self) -> int:
        return self.uncached + self.cached + self.cache_creation + self.output


@dataclass(frozen=True)
class UsageRecord:
    provider: str
    timestamp_ms: int
    model: str
    rate_model: Optional[str]
    session_id: str
    tokens: Tokens
    reported_cost_usd: Optional[float]
    speed: str
    dedupe_key: Optional[str]


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _safe_int(value: Any) -> bool:
    return _num(value) and float(value).is_integer() and abs(value) <= 2 ** 53 - 1


def _valid_position(entry: Dict[str, Any]) -> bool:
    """T3's check on a file's read position (`o` offset, `gl` guard length, `gh` guard hash)."""
    o, gl, gh = entry.get("o"), entry.get("gl"), entry.get("gh")
    return _safe_int(o) and o >= 0 and _safe_int(gl) and 0 <= gl <= 64 and gl <= o and _num(gh)


def _refuse_newer_version(folder: Path, stem: str, version: int) -> None:
    """T3 writes each cache version to its own file name and leaves the old one behind, frozen.
    A newer file next to ours means the one we read has stopped changing."""
    for sibling in folder.glob(stem + "*.json"):
        suffix = sibling.name[len(stem):-len(".json")]
        if suffix.isdigit() and int(suffix) > version:
            raise CacheError("T3 Code now writes %s; this card reads only version %d" % (sibling.name, version))


def _read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise CacheError("cache file is missing: %s" % path.name)
    except (OSError, ValueError) as error:
        raise CacheError("cache file cannot be read: %s (%s)" % (path.name, type(error).__name__))


def _decode_scan_rows(rows: Any, provider: str, models: List[str], sessions: List[str]) -> Optional[List[UsageRecord]]:
    """Mirror of T3's `decodeRecords`: one bad row invalidates the whole file."""
    if not isinstance(rows, list):
        return None
    records = []  # type: List[UsageRecord]
    for row in rows:
        if not isinstance(row, list) or len(row) < 11:
            return None
        ts, model_idx, session_idx, uncached, cached, creation, output, reasoning, key, cost, speed_idx = row[:11]
        rate_idx = row[11] if len(row) > 11 else None
        speed = SPEEDS[speed_idx] if isinstance(speed_idx, int) and not isinstance(speed_idx, bool) and 0 <= speed_idx < len(SPEEDS) else None
        model = models[model_idx] if isinstance(model_idx, int) and 0 <= model_idx < len(models) else None
        rate_model = models[rate_idx] if isinstance(rate_idx, int) and 0 <= rate_idx < len(models) else None
        if (not _num(ts) or model is None or (rate_idx is not None and rate_model is None)
                or not all(_num(v) for v in (uncached, cached, creation, output, reasoning)) or speed is None):
            return None
        session = sessions[session_idx] if isinstance(session_idx, int) and 0 <= session_idx < len(sessions) else ""
        records.append(UsageRecord(
            provider=provider,
            timestamp_ms=int(ts),
            model=model,
            rate_model=rate_model,
            session_id=session,
            tokens=Tokens(int(uncached), int(cached), int(creation), int(output), int(reasoning)),
            reported_cost_usd=float(cost) if _num(cost) else None,
            speed=speed,
            dedupe_key=key if isinstance(key, str) else None,
        ))
    return records


def load_scan_cache(path: Path, min_file_mtime_ms: float = 0) -> List[List[UsageRecord]]:
    """Records per transcript file, in cache order. Files older than `min_file_mtime_ms` are left out, as T3 does."""
    _refuse_newer_version(path.parent, "usage-scan-cache-v", SCAN_CACHE_VERSION)
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("version") != SCAN_CACHE_VERSION:
        raise CacheError("unsupported scan cache version: %r" % (document.get("version") if isinstance(document, dict) else None))
    models, sessions, files = document.get("models"), document.get("sessions"), document.get("files")
    if (not isinstance(models, list) or not isinstance(sessions, list) or not isinstance(files, dict)
            or not all(isinstance(m, str) for m in models) or not all(isinstance(s, str) for s in sessions)):
        raise CacheError("scan cache has an unexpected shape")
    per_file = []  # type: List[List[UsageRecord]]
    for entry in files.values():
        if not isinstance(entry, dict) or not _num(entry.get("s")) or not _num(entry.get("m")):
            continue
        provider = entry.get("p")
        if not isinstance(provider, str) or entry["m"] < min_file_mtime_ms or not _valid_position(entry):
            continue
        head = _decode_scan_rows(entry.get("r"), provider, models, sessions)
        tail = _decode_scan_rows(entry.get("t"), provider, models, sessions)
        if head is None or tail is None:
            continue
        per_file.append(head + tail)
    return per_file


def cursor_rate_model(model: str) -> str:
    """T3's `cursorRateModel`: Cursor's tiered names to the base model's rate-table key."""
    base = re.sub(r"^cursor-", "", model, count=1)
    base = re.sub(r"(?:-thinking)?(?:-(?:none|minimal|low|medium|high|xhigh|max))?(?:-fast)?$", "", base, count=1)
    return "xai/" + base if base.startswith("grok-") else base


def cursor_usage_enabled(settings_path: Path) -> bool:
    """T3's `cursorKeychainUsageEnabled` (default off). When off, T3 shows no Cursor account usage."""
    if not settings_path.exists():
        return False
    settings = _read_json(settings_path)
    return isinstance(settings, dict) and settings.get("cursorKeychainUsageEnabled") is True


def load_cursor_cache(path: Path) -> List[UsageRecord]:
    """Cursor's account usage. A missing file means no Cursor login, so no records.

    T3 reads only the signed-in credential's cache, so when the file holds several accounts
    (an old login left behind), only the most recently fetched one counts."""
    _refuse_newer_version(path.parent, "usage-cursor-account-cache-v", CURSOR_CACHE_VERSION)
    if not path.exists():
        return []
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("version") != CURSOR_CACHE_VERSION:
        raise CacheError("unsupported cursor cache version")
    strings, accounts = document.get("strings"), document.get("accounts")
    if not isinstance(strings, list) or not isinstance(accounts, dict) or not all(isinstance(s, str) for s in strings):
        raise CacheError("cursor cache has an unexpected shape")

    def at(index: Any) -> Optional[str]:
        return strings[index] if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(strings) else None

    latest = None  # type: Optional[Tuple[float, List[UsageRecord]]]
    for account in accounts.values():
        if (not isinstance(account, dict) or not isinstance(account.get("accountKey"), str)
                or not all(_num(account.get(field)) for field in ("sinceMs", "untilMs", "fetchedAtMs"))
                or not isinstance(account.get("records"), list)):
            continue
        rows = account["records"]
        decoded = []  # type: List[UsageRecord]
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                break
            ts, model_idx, session_idx, uncached, cached, creation, output, cost, key = row[:9]
            model, session = at(model_idx), at(session_idx)
            if (not _num(ts) or model is None or session is None or not all(_num(v) for v in (uncached, cached, creation, output))
                    or (cost is not None and not _num(cost)) or (key is not None and not isinstance(key, str))):
                break
            decoded.append(UsageRecord("cursor", int(ts), model, cursor_rate_model(model), session,
                                       Tokens(int(uncached), int(cached), int(creation), int(output), 0),
                                       float(cost) if cost is not None else None, "standard", key))
        else:  # a malformed account is dropped whole, as T3 does
            if latest is None or account["fetchedAtMs"] > latest[0]:
                latest = (account["fetchedAtMs"], decoded)
    return latest[1] if latest else []


def _shared_sessions(per_file: List[List[UsageRecord]]) -> Set[str]:
    first_file = {}  # type: Dict[str, int]
    shared = set()  # type: Set[str]
    for index, records in enumerate(per_file):
        previous = ""
        for record in records:
            if record.provider not in SHARED_SESSION_PROVIDERS or record.session_id == previous or not record.session_id:
                continue
            previous = record.session_id
            first = first_file.setdefault(record.session_id, index)
            if first != index:
                shared.add(record.session_id)
    return shared


def _shared_session_key(record: UsageRecord, occurrence: int) -> str:
    t = record.tokens
    identity = json.dumps([record.provider, record.session_id, record.timestamp_ms, record.model,
                           {"u": t.uncached, "c": t.cached, "w": t.cache_creation, "o": t.output, "r": t.reasoning}],
                          separators=(",", ":"))
    return "%s:%d" % (identity, occurrence)


def dedupe(per_file: List[List[UsageRecord]]) -> List[UsageRecord]:
    """Global de-duplication by dedupe key, first copy wins, exactly as T3's aggregator does.

    Claude Code copies a message's records forward when a session is resumed or forked, so the
    same `message.id:requestId` key legitimately appears in several transcripts.
    """
    shared = _shared_sessions(per_file)
    seen = set()  # type: Set[str]
    kept = []  # type: List[UsageRecord]
    for records in per_file:
        occurrences = {}  # type: Dict[Tuple[Any, ...], int]
        for record in records:
            key = record.dedupe_key
            if record.provider in SHARED_SESSION_PROVIDERS and record.session_id in shared:
                t = record.tokens
                identity = (record.provider, record.session_id, record.timestamp_ms, record.model, t)
                occurrences[identity] = occurrences.get(identity, 0) + 1
                key = _shared_session_key(record, occurrences[identity])
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            kept.append(record)
    return kept


def load_records(userdata_dir: Path, min_file_mtime_ms: float = 0) -> List[UsageRecord]:
    """Every usage record T3 Code knows about, de-duplicated. Raises CacheError if the scan cache is unusable."""
    per_file = load_scan_cache(userdata_dir / SCAN_CACHE_FILE, min_file_mtime_ms)
    enabled = cursor_usage_enabled(userdata_dir / SETTINGS_FILE)
    cursor = load_cursor_cache(userdata_dir / CURSOR_CACHE_FILE) if enabled else []
    return dedupe(per_file + [cursor])
