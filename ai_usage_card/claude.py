"""Claude Code usage, read straight from its transcripts (`~/.claude/projects/**/*.jsonl`).

T3 Code's scan cache is not used for Claude Code because it undercounts it in two ways:

* Claude Code writes an assistant reply to the transcript several times while it streams, and only
  the last copy carries the final `usage.output_tokens`. T3 keeps the first copy.
* T3 prices every cache write at the 5-minute rate; `usage.cache_creation.ephemeral_1h_input_tokens`
  are billed higher.

Only the model name, the timestamp and the token counts are taken from a line; no text, path or id
leaves this module (ids are used to de-duplicate and are not stored on the records).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .load import CacheError, Tokens, UsageRecord, _num

PROJECTS_DIR = Path.home() / ".claude" / "projects"
_FRACTION = re.compile(r"\.(\d+)")


def _timestamp_ms(value: Any) -> Optional[int]:
    """ISO 8601 to epoch milliseconds. Python 3.9 reads 3 or 6 fraction digits only, so pad or cut."""
    if not isinstance(value, str):
        return None
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], value.replace("Z", "+00:00"), count=1)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp() * 1000)


def _count(value: Any) -> int:
    return int(value) if _num(value) and value > 0 else 0


def _unreadable(error: OSError) -> None:
    # A folder that cannot be listed would drop its usage from the card without a trace.
    raise CacheError("a Claude Code transcripts folder cannot be read (%s)" % type(error).__name__)


def transcript_files(roots: Iterable[Path], min_mtime_ms: float = 0) -> Iterator[str]:
    """Every transcript under `roots`, each real directory and file once. Some project folders are
    symlinks to others (and a root itself may be one, or the same folder as another root), so
    directories and files are tracked by real path, which also stops a symlink cycle.

    A root that does not exist is skipped (the caller refuses when none does); a folder or file that
    exists but cannot be read raises CacheError, so the card is never published short."""
    seen_dirs = set()  # type: Set[str]
    seen_files = set()  # type: Set[str]
    for root in roots:
        if not os.path.isdir(str(root)):
            continue
        for folder, subfolders, names in os.walk(str(root), onerror=_unreadable, followlinks=True):
            real = os.path.realpath(folder)
            if real in seen_dirs:
                subfolders[:] = []
                continue
            seen_dirs.add(real)
            subfolders.sort()
            for name in sorted(names):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.realpath(os.path.join(real, name))
                if path in seen_files:
                    continue
                seen_files.add(path)
                try:
                    if os.stat(path).st_mtime * 1000 < min_mtime_ms:
                        continue
                except FileNotFoundError:  # a broken symlink, or removed since the folder was listed
                    continue
                except OSError as error:
                    _unreadable(error)
                yield path


def _record_from_line(line: str) -> Optional[Tuple[Tuple[Any, ...], UsageRecord]]:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return None
    message = row.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    model, ts = message.get("model"), _timestamp_ms(row.get("timestamp"))
    if not isinstance(model, str) or not model or model == "<synthetic>" or ts is None:
        return None
    split = usage.get("cache_creation")
    five, hour = (split.get("ephemeral_5m_input_tokens"), split.get("ephemeral_1h_input_tokens")) if isinstance(split, dict) else (None, None)
    if five is None and hour is None:
        five, hour = usage.get("cache_creation_input_tokens"), 0
    five, hour = _count(five), _count(hour)
    tokens = Tokens(_count(usage.get("input_tokens")), _count(usage.get("cache_read_input_tokens")),
                    five + hour, _count(usage.get("output_tokens")), 0, hour)
    if not tokens.total:
        return None
    # A message id is unique to one API response, and some lines carry it without a request id, so
    # it is the key on its own; the request id is used only when the message id is missing.
    msg_id, request_id = message.get("id"), row.get("requestId")
    key = (("m", msg_id) if isinstance(msg_id, str) and msg_id
           else ("r", request_id) if isinstance(request_id, str) and request_id else None)
    speed = "fast" if usage.get("speed") == "fast" else "standard"
    return key, UsageRecord("claude", ts, model, None, "", tokens, None, speed, None)


def load_claude_records(roots: Iterable[Path] = (PROJECTS_DIR,), min_file_mtime_ms: float = 0) -> List[UsageRecord]:
    """One record per assistant reply. A reply is written several times (streaming) and a resumed
    or forked chat copies its history, so a reply is kept once, as the copy with the most output.
    Raises CacheError when no root exists or a transcript cannot be read."""
    roots = list(roots)
    if not any(os.path.isdir(str(root)) for root in roots):
        raise CacheError("no Claude Code transcripts folder found")
    best = {}  # type: Dict[Tuple[Any, ...], UsageRecord]
    unkeyed = []  # type: List[UsageRecord]
    for path in transcript_files(roots, min_file_mtime_ms):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    found = _record_from_line(line)
                    if found is None:
                        continue
                    key, record = found
                    if key is None:
                        unkeyed.append(record)
                    elif key not in best or record.tokens.output > best[key].tokens.output:
                        best[key] = record
        except FileNotFoundError:  # removed since the folder was listed
            continue
        except OSError as error:
            _unreadable(error)
    return list(best.values()) + unkeyed
