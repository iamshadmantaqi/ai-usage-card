"""Read T3 Code's usage caches into typed records.

The record layouts below were read from T3 Code's own source (the bundled server,
`usageScanCache.ts` and `accountUsage.ts` in `provider-cursor`), not guessed.

Scan cache `usage-scan-cache-v<N>.json`, N from 5 to 17 (checked against T3's bundled server in November
2026; T3 writes each version to its own file and deletes the older ones once the new one is saved)::

    {sources, coveredSince, version: N, models: [name], sessions: [id], files: {path: entry}}
    entry  = {s: size, m: mtimeMs, p: provider, r: [row], t: [row], mr, o, gl, gh, cs}
    row    = [timestampMs, modelIdx, sessionIdx, uncachedInput, cachedInput,
              cacheCreation, output, reasoning, dedupeKey, reportedCostUsd,
              speedIdx, rateModelIdx, serviceTierTurnId, cacheCreation1h]   (speedIdx: 0 standard, 1 fast, 2 ultrafast)

Versions 6 to 17 left the first twelve row fields alone. Version 7 added the optional thirteenth (a
string or null, not a usage figure) and `mr` (a count of malformed lines in the file). Between 9 and 17
T3 added a fourteenth, `cacheCreation1h` (the part of `cacheCreation` written with the 1-hour lifetime;
T3 writes it only when set, with the thirteenth before it, null when there is no turn id; T3 prices it at
the 1-hour rate and `min(cacheCreation1h, cacheCreation)` of `cacheCreation` at the 5-minute rate), `mt` (the
malformed lines' times) and, at the top level, `coveredSince` (per source, how far back T3 has scanned).
Version 17 adds the record's time to Grok dedupe keys read from older files, as `_decode_scan_rows` does.
The other changes, up to 17, are to how T3 resumes reading Codex, Grok and Claude files. `coveredSince`,
`mr`, `mt` and the resume state are not read here.

A version above the last one checked (`SCAN_CACHE_CHECKED_THROUGH`) is read anyway when its structure
matches this layout, with a warning; it is refused when a file entry or row does not match, or a row is
wider than fourteen fields.

Cursor cache `usage-cursor-account-cache-v<N>.json`, N 1 or 2 (checked against T3's bundled server in November
2026)::

    {version: N, strings: [str], accounts: {key: {accountKey, sinceMs, untilMs,
     fetchedAtMs, records: [row]}}}
    row    = [timestampMs, modelStrIdx, sessionStrIdx, uncachedInput, cachedInput,
              cacheCreation, output, reportedCostUsd, dedupeKey]

Version 2 changed only the key of `accounts`, from the credential's file to a digest of each API key; the
rows are the same. T3 no longer has a setting that switches Cursor usage off (`cursorKeychainUsageEnabled`
is gone from its code): it reads every Cursor API key it holds. T3 leaves an entry in the file after it stops
holding that key and then no longer shows its usage; the card still counts it, so every account in the file
counts.

Reasoning tokens are a subset of output tokens, so they are never added to a total.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger("ai-usage-card")

SCAN_CACHE_STEM = "usage-scan-cache-v"
CURSOR_CACHE_STEM = "usage-cursor-account-cache-v"
SCAN_CACHE_MIN_VERSION = 5
SCAN_CACHE_CHECKED_THROUGH = 17
SCAN_ROW_FIELDS = 14  # the widest row in a checked version
CURSOR_CACHE_MIN_VERSION = 1
CURSOR_CACHE_VERSION = 2
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
    # 5-minute default), never added to a total. T3 records it only for Claude Code, in cache versions
    # after 9; the card reads Claude Code from its transcripts, so a T3 record's figure is not used.
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


def _js_number(value: float) -> str:
    """A whole number as JavaScript's template strings print it ("1700000000000", not "1.7e12")."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _decode_scan_rows(rows: Any, provider: str, models: List[str], sessions: List[str], version: int,
                      strict: bool) -> Optional[List[UsageRecord]]:
    """Mirror of T3's `decodeRecords`: one bad row invalidates the whole file. `strict` (a version not checked
    against T3's source) also rejects a row wider than the layout above, as an extra field could change a total."""
    if not isinstance(rows, list):
        return None
    records = []  # type: List[UsageRecord]
    for row in rows:
        if not isinstance(row, list) or len(row) < 11 or (strict and len(row) > SCAN_ROW_FIELDS):
            return None
        ts, model_idx, session_idx, uncached, cached, creation, output, reasoning, key, cost, speed_idx = row[:11]
        rate_idx = row[11] if len(row) > 11 else None
        turn_id = row[12] if len(row) > 12 else None
        one_hour = row[13] if len(row) > 13 else 0  # T3 defaults only a missing one; a null drops the file
        speed = SPEEDS[speed_idx] if isinstance(speed_idx, int) and not isinstance(speed_idx, bool) and 0 <= speed_idx < len(SPEEDS) else None
        model = models[model_idx] if isinstance(model_idx, int) and 0 <= model_idx < len(models) else None
        rate_model = models[rate_idx] if isinstance(rate_idx, int) and 0 <= rate_idx < len(models) else None
        if (not _num(ts) or model is None or (rate_idx is not None and rate_model is None)
                or (turn_id is not None and not isinstance(turn_id, str))
                or not all(_num(v) for v in (uncached, cached, creation, output, reasoning, one_hour)) or speed is None):
            return None
        session = sessions[session_idx] if isinstance(session_idx, int) and 0 <= session_idx < len(sessions) else ""
        if (isinstance(key, str) and provider == "grok" and version < 17 and key.endswith(":" + model)
                and not key.endswith(":%s:%s" % (_js_number(ts), model))):
            # T3 17 adds the time to Grok keys written before it, so two replies no longer share one key.
            key = "%s%s:%s" % (key[:-len(model)], _js_number(ts), model)
        records.append(UsageRecord(
            provider=provider,
            timestamp_ms=int(ts),
            model=model,
            rate_model=rate_model,
            session_id=session,
            tokens=Tokens(int(uncached), int(cached), int(creation), int(output), int(reasoning), int(one_hour)),
            reported_cost_usd=float(cost) if _num(cost) else None,
            speed=speed,
            dedupe_key=key if isinstance(key, str) else None,
        ))
    return records


def scan_cache_path(folder: Path) -> Tuple[Path, int]:
    """The scan cache file T3 saved last, and its version. T3 gives each cache version its own file name
    and deletes the old ones once the new one is saved, so there is normally one. Two are left when an
    older T3 shares the folder (it cannot read the newer file, so it leaves it frozen and writes its own):
    the one saved last is the live one, and the higher version wins a tie. Files below version 5 are
    never read. A version above
    `SCAN_CACHE_CHECKED_THROUGH` is returned too; `_read_scan_document` warns and checks its structure."""
    found = {}  # type: Dict[int, Tuple[float, Path]]
    for candidate in folder.glob(SCAN_CACHE_STEM + "*.json"):
        # ASCII digits with no leading zero, as T3 writes them; int() would reject other Unicode digits.
        number = candidate.name[len(SCAN_CACHE_STEM):-len(".json")]
        if re.fullmatch(r"[1-9][0-9]*", number):
            try:
                found[int(number)] = (candidate.stat().st_mtime, candidate)
            except FileNotFoundError:  # removed by T3 between the listing and the stat
                continue
    if not found:
        raise CacheError("cache file is missing: %s%d.json" % (SCAN_CACHE_STEM, SCAN_CACHE_CHECKED_THROUGH))
    readable = [version for version in found if version >= SCAN_CACHE_MIN_VERSION]
    if not readable:
        raise CacheError("cache file is missing: %s%d.json (only an older %s is there)"
                         % (SCAN_CACHE_STEM, SCAN_CACHE_CHECKED_THROUGH, found[max(found)][1].name))
    live = max(readable, key=lambda version: (found[version][0], version))
    return found[live][1], live


def _read_scan_document(path: Path, version: int) -> Dict[str, Any]:
    document = _read_json(path)
    # The layout follows the version, so the file must say the version its name promises.
    if not isinstance(document, dict) or document.get("version") != version:
        raise CacheError("unsupported scan cache version: %r" % (document.get("version") if isinstance(document, dict) else None))
    models, sessions, files = document.get("models"), document.get("sessions"), document.get("files")
    if (not isinstance(models, list) or not isinstance(sessions, list) or not isinstance(files, dict)
            or not all(isinstance(m, str) for m in models) or not all(isinstance(s, str) for s in sessions)):
        raise CacheError("scan cache has an unexpected shape")
    if version > SCAN_CACHE_CHECKED_THROUGH:
        log.warning("T3 Code writes %s, newer than the version %d this card was checked against; "
                    "reading it, its top-level structure matches", path.name, SCAN_CACHE_CHECKED_THROUGH)
    return document


def _scan_records(document: Dict[str, Any], min_file_mtime_ms: float) -> List[List[UsageRecord]]:
    """Like T3, a bad file entry or row only drops that file. In a version not checked against T3's
    source (`strict`) it means the layout changed, so the cache is refused instead."""
    strict = document["version"] > SCAN_CACHE_CHECKED_THROUGH
    models, sessions = document["models"], document["sessions"]
    per_file = []  # type: List[List[UsageRecord]]
    for entry in document["files"].values():
        if (not isinstance(entry, dict) or not _num(entry.get("s")) or not _num(entry.get("m"))
                or not isinstance(entry.get("p"), str) or not _valid_position(entry)):
            if strict:
                raise CacheError("scan cache version %d has a file entry this card cannot read" % document["version"])
            continue
        provider = entry["p"]
        if entry["m"] < min_file_mtime_ms:
            continue
        head = _decode_scan_rows(entry.get("r"), provider, models, sessions, document["version"], strict)
        tail = _decode_scan_rows(entry.get("t"), provider, models, sessions, document["version"], strict)
        if head is None or tail is None:
            if strict:
                raise CacheError("scan cache version %d has rows this card cannot read" % document["version"])
            continue
        per_file.append(head + tail)
    return per_file


def _claude_folders(document: Dict[str, Any]) -> List[str]:
    """The Claude Code transcript folders T3 scans: `sources` maps "claude\\0<folder>" to {dir: <real folder>}."""
    sources = document.get("sources")
    folders = []  # type: List[str]
    for key, value in (sources.items() if isinstance(sources, dict) else ()):
        provider, _, folder = key.partition("\0") if isinstance(key, str) else ("", "", "")
        if provider != "claude":
            continue
        for candidate in (folder, value.get("dir") if isinstance(value, dict) else None):
            if isinstance(candidate, str) and candidate and candidate not in folders:
                folders.append(candidate)
    return folders


def cursor_rate_model(model: str) -> str:
    """T3's `cursorRateModel`: Cursor's tiered names to the base model's rate-table key."""
    base = re.sub(r"^cursor-", "", model, count=1)
    base = re.sub(r"(?:-thinking)?(?:-(?:none|minimal|low|medium|high|xhigh|max))?(?:-fast)?$", "", base, count=1)
    return "xai/" + base if base.startswith("grok-") else base


def cursor_cache_path(userdata_dir: Path) -> Path:
    """The newest Cursor cache file there is: version 2, else the version 1 file an older T3 left behind."""
    for version in range(CURSOR_CACHE_VERSION, CURSOR_CACHE_MIN_VERSION - 1, -1):
        candidate = userdata_dir / (CURSOR_CACHE_STEM + "%d.json" % version)
        if candidate.exists():
            return candidate
    return userdata_dir / (CURSOR_CACHE_STEM + "%d.json" % CURSOR_CACHE_VERSION)


def _cursor_accounts(path: Path) -> List[Tuple[float, List[UsageRecord], int, int]]:
    """The most recently fetched entry of each Cursor account as (fetchedAtMs, records, sinceMs, untilMs)."""
    _refuse_newer_version(path.parent, CURSOR_CACHE_STEM, CURSOR_CACHE_VERSION)
    if not path.exists():
        return []
    document = _read_json(path)
    version = document.get("version") if isinstance(document, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or not CURSOR_CACHE_MIN_VERSION <= version <= CURSOR_CACHE_VERSION:
        raise CacheError("unsupported cursor cache version")
    strings, accounts = document.get("strings"), document.get("accounts")
    if not isinstance(strings, list) or not isinstance(accounts, dict) or not all(isinstance(s, str) for s in strings):
        raise CacheError("cursor cache has an unexpected shape")

    def at(index: Any) -> Optional[str]:
        return strings[index] if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(strings) else None

    latest = {}  # type: Dict[str, Tuple[float, List[UsageRecord], int, int]]
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
            kept = latest.get(account["accountKey"])
            if kept is None or account["fetchedAtMs"] > kept[0]:
                latest[account["accountKey"]] = (account["fetchedAtMs"], decoded, int(account["sinceMs"]), int(account["untilMs"]))
    return list(latest.values())


def load_cursor_coverage(path: Path) -> Tuple[List[UsageRecord], Optional[int], Optional[int]]:
    """The records `load_cursor_account` reads, and the span every account covers: the latest `sinceMs` and the
    earliest `untilMs` (None, None without an account)."""
    accounts = _cursor_accounts(path)
    records = [r for _, decoded, _, _ in accounts for r in decoded]
    if not accounts:
        return records, None, None
    return records, max(a[2] for a in accounts), min(a[3] for a in accounts)


def load_cursor_account(path: Path, live_since_ms: Optional[float] = None) -> Tuple[List[UsageRecord], Optional[int]]:
    """Cursor's account usage and the time it is complete up to (`untilMs`); no records and None without a file.

    The file holds one entry per API key. T3 turns each into a usage source named after its `accountKey`,
    keeping one source per account (two keys of one account count once), and its aggregator then drops
    repeated dedupe keys; so every account counts, the most recently fetched entry of each, and `dedupe`
    does the rest. T3 builds every dedupe key from the account (`cursor-account:<accountKey>:...`), so two
    accounts never share one, and the same usage cannot be counted under two of them.

    The time returned is the earliest `untilMs` of the accounts fetched at or after `live_since_ms` (all of
    them when it is None), so a day is held back from freezing until every account still being read has
    reached past it. T3 fetches every key it holds on each scan and never deletes an entry, so an account
    not fetched since `live_since_ms` is a key T3 no longer holds or has failed to read for that long: its
    records still count, but it cannot hold freezing back, because nothing will ever move its end. None
    when no account was fetched since then."""
    accounts = _cursor_accounts(path)
    records = [r for _, decoded, _, _ in accounts for r in decoded]
    live = [until for fetched, _, _, until in accounts if live_since_ms is None or fetched >= live_since_ms]
    return records, min(live) if live else None


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
    same `message.id:requestId` key legitimately appears in several transcripts. (The card reads
    Claude Code from its transcripts instead, see `claude.py`; T3's Claude records are only checked
    for presence.)
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


def load_t3(userdata_dir: Path, min_file_mtime_ms: float = 0,
            cursor_live_since_ms: Optional[float] = None) -> Tuple[List[UsageRecord], List[str], Optional[int]]:
    """Every usage record T3 Code knows about, de-duplicated, the Claude Code transcript folders
    it scans (used only to read those folders, never stored), and the time Cursor's account cache is
    complete up to (None when Cursor has no cache or no account fetched since `cursor_live_since_ms`, see
    `load_cursor_account`). Raises CacheError if the scan cache is unusable."""
    document = _read_scan_document(*scan_cache_path(userdata_dir))
    per_file = _scan_records(document, min_file_mtime_ms)
    cursor, cursor_until = load_cursor_account(cursor_cache_path(userdata_dir), cursor_live_since_ms)
    return dedupe(per_file + [cursor]), _claude_folders(document), cursor_until


def load_records(userdata_dir: Path, min_file_mtime_ms: float = 0) -> List[UsageRecord]:
    """Every usage record T3 Code knows about, de-duplicated. Raises CacheError if the scan cache is unusable."""
    return load_t3(userdata_dir, min_file_mtime_ms)[0]
