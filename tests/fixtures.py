"""A small, hand-made T3 Code userdata folder. Every expected number in the tests is worked out
by hand from this data (see the comments), not computed by the code under test."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ai_usage_card.summarise import TIMEZONE

NOW = datetime(2026, 10, 18, 4, 0, tzinfo=TIMEZONE)  # "today" is 18 Oct 2026 in Dhaka


def ms(month: int, day: int, hour: int) -> int:
    return int(datetime(2026, month, day, hour, 0, tzinfo=TIMEZONE).timestamp() * 1000)


MTIME = ms(10, 18, 3)
MODELS = ["claude-opus-5-5", "<synthetic>", "claude-sonnet-5-5", "gpt-6-luna", "mystery-model",
          "tiny-1", "tiny-2", "tiny-3"]
SESSIONS = ["s-claude", "s-codex", "s-grok"]
STANDARD, FAST = 0, 1


def row(ts, model, session, uncached, cached, creation, output, key=None, cost=None, speed=STANDARD):
    return [ts, MODELS.index(model), session, uncached, cached, creation, output, 0, key, cost, speed, None]


def entry(provider, rows):
    return {"s": 1000, "m": MTIME, "p": provider, "r": rows, "t": [], "o": 1000, "gl": 64, "gh": 1, "cs": None}


A1 = row(ms(10, 17, 12), "claude-opus-5-5", 0, 1000, 2000, 3000, 4000, "msg_a:req_a")  # 10000 tokens, $0.02495
A2 = row(ms(10, 17, 13), "claude-opus-5-5", 0, 100, 0, 0, 200, "msg_b:req_b", speed=FAST)  # 300 tokens, $0.0022 (fast = 2x)
A3 = row(ms(10, 17, 14), "<synthetic>", 0, 5000, 0, 0, 0, "msg_s:req_s")  # excluded
A4 = row(ms(10, 16, 9), "claude-sonnet-5-5", 0, 500, 10000, 0, 500, "msg_c:req_c")  # 11000 tokens, $0.007
A5 = row(ms(9, 1, 9), "claude-opus-5-5", 0, 999999, 0, 0, 0, "msg_old:req_old")  # before the window
B1 = row(ms(10, 18, 1), "claude-opus-5-5", 0, 10, 0, 0, 10, "msg_d:req_d", cost=0.5)  # 20 tokens, reported $0.5
CODEX = row(ms(10, 18, 2), "gpt-6-luna", 1, 100, 200, 0, 50)  # 350 tokens, $0.000055 (cache falls back to input rate)
E1 = row(ms(10, 15, 9), "mystery-model", 2, 1000, 0, 0, 1000)  # 2000 tokens, unpriced: $0
E2 = [row(ms(10, 15, 10), "tiny-%d" % n, 2, n, 0, 0, 0) for n in (1, 2, 3)]  # 1, 2 and 3 tokens


def scan_cache(version=5):
    return {
        "sources": {}, "version": version, "models": MODELS, "sessions": SESSIONS,
        "files": {
            "/f/a.jsonl": entry("claude", [A1, A2, A3, A4, A5]),
            "/f/b.jsonl": entry("claude", [A1, B1]),  # a fork that copied A1 forward
            "/f/c.jsonl": entry("codex", [CODEX]),
            "/f/d.jsonl": entry("codex", [CODEX]),  # the same rollout read from a second file
            "/f/e.jsonl": entry("grok", [E1] + E2),
        },
    }


def scan_cache_v9():
    """The layout T3 writes from version 7: `mr` on every file and, on a row, a thirteenth field
    (a service tier turn id, a string) after the rate model. Same usage figures as `scan_cache()`."""
    cache = scan_cache(version=9)
    for file_entry in cache["files"].values():
        file_entry["mr"] = 0
    cache["files"]["/f/a.jsonl"]["r"] = [r + ["turn-%d" % n] for n, r in enumerate([A1, A2, A3, A4, A5])]
    cache["files"]["/f/b.jsonl"]["r"] = [A1 + ["turn-0"], B1 + [None]]
    cache["files"]["/f/c.jsonl"]["r"] = [CODEX + ["turn-1"]]
    cache["files"]["/f/d.jsonl"]["r"] = [CODEX + ["turn-1"]]  # grok's rows stay twelve wide, as T3 mixes both
    return cache


def cursor_cache():
    strings = ["cursor-grok-4.6-high-fast", "agent-session"]
    # 3500 tokens; reported $0.0123 replaces the list price of $0.009
    rows = [[ms(10, 18, 3), 0, 1, 1000, 2000, 0, 500, 0.0123, "cursor-account:k:1:0"]]
    return {"version": 1, "strings": strings, "accounts": {"keychain": {
        "accountKey": "k", "sinceMs": 0, "untilMs": 1, "fetchedAtMs": 1, "records": rows}}}


def rates():
    return {"fetchedAtMs": 1, "document": {
        "claude-opus-5-5": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                            "cache_read_input_token_cost": 1e-7, "cache_creation_input_token_cost": 1.25e-6,
                            "cache_creation_input_token_cost_above_1hr": 2e-6,
                            "provider_specific_entry": {"fast": 2}},
        "claude-sonnet-5-5": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5,
                              "cache_read_input_token_cost": 1e-7, "cache_creation_input_token_cost": 2.5e-6},
        "openai/gpt-6-luna": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
        "xai/grok-4.6": {"input_cost_per_token": 2e-6, "output_cost_per_token": 6e-6},
    }}


def write_userdata(folder: Path, scan=None, cursor=None, rate_table=None, cursor_enabled=True, scan_version=5) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"cursorKeychainUsageEnabled": cursor_enabled}))
    (folder / ("usage-scan-cache-v%d.json" % scan_version)).write_text(json.dumps(scan if scan is not None else scan_cache(scan_version)))
    (folder / "usage-cursor-account-cache-v1.json").write_text(json.dumps(cursor if cursor is not None else cursor_cache()))
    (folder / "usage-model-rates.json").write_text(json.dumps(rate_table if rate_table is not None else rates()))
    return folder


def reply(model, when, usage, msg="msg_x", req="req_x", kind="assistant"):
    """One transcript line. `when` is (month, day, hour[, year]) in UTC; `req=None` leaves the request id out. Extra keys mimic Claude Code's real lines."""
    ts = "%04d-%02d-%02dT%02d:00:00.123Z" % (when[3] if len(when) > 3 else 2026, when[0], when[1], when[2])  # 3 digits, as Claude Code writes
    line = {"type": kind, "timestamp": ts, "uuid": "u-%s" % msg, "cwd": "/Users/someone/secret",
            "message": {"id": msg, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "private words"}], "usage": usage}}
    if req is not None:
        line["requestId"] = req
    return json.dumps(line)


def usage(inp, out, read=0, five=None, hour=None, whole=None, speed=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read}
    if five is not None or hour is not None:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": five or 0, "ephemeral_1h_input_tokens": hour or 0}
    if whole is not None:
        u["cache_creation_input_tokens"] = whole
    if speed:
        u["speed"] = speed
    return u


def write_lines(path: Path, lines, mtime_ms=MTIME):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.utime(str(path), (mtime_ms / 1000, mtime_ms / 1000))


def write_projects(folder: Path) -> Path:
    """Claude Code transcripts. Worked out by hand with the rates() above:

    X (opus, 17 Oct) is written three times, two in one chat and the last in a resumed chat; the final copy has 100 uncached,
      1000 read, 400 5-minute and 600 1-hour writes, 3000 output = 5100 tokens, and costs
      0.0001 + 0.0001 + 0.0005 + 0.0012 + 0.015 = $0.0169 (the 600 1-hour tokens at 2e-6, not 1.25e-6).
    Y (sonnet, in a subagent file, 16 Oct): 50 + 20 output + 100 writes with no split (5-minute) = 170 tokens, $0.00055.
    F (opus, fast, 17 Oct): 100 in, 200 out = 300 tokens, (0.0001 + 0.001) * 2 = $0.0022.
    Also present and ignored: a synthetic reply, a user line, a line without usage, a broken line, a symlinked copy of the
    project folder, and a file last written before the window."""
    folder.mkdir(parents=True, exist_ok=True)
    final = usage(100, 3000, read=1000, five=400, hour=600)
    write_lines(folder / "-proj-a" / "chat1.jsonl", [
        reply("claude-opus-5-5", (10, 17, 6), usage(100, 10, read=1000, five=400, hour=600)),
        reply("claude-opus-5-5", (10, 17, 6), usage(100, 200, read=1000, five=400, hour=600)),
        reply("claude-opus-5-5", (10, 17, 7), usage(100, 200, speed="fast"), msg="msg_f", req="req_f"),
        reply("<synthetic>", (10, 17, 7), usage(999, 0), msg="msg_s", req="req_s"),
        reply("claude-opus-5-5", (10, 17, 7), usage(777, 777), msg="msg_u", req="req_u", kind="user"),
        json.dumps({"type": "assistant", "timestamp": "2026-10-17T07:00:00.000Z", "message": {"id": "m", "model": "claude-opus-5-5"}}),
        "{broken",
    ])
    write_lines(folder / "-proj-a" / "chat2.jsonl", [reply("claude-opus-5-5", (10, 17, 6), final)])
    write_lines(folder / "-proj-a" / "chat1" / "subagents" / "agent-1.jsonl",
                [reply("claude-sonnet-5-5", (10, 16, 5), usage(50, 20, whole=100), msg="msg_y", req="req_y")])
    write_lines(folder / "-proj-old" / "old.jsonl", [reply("claude-opus-5-5", (8, 1, 5), usage(1, 1), msg="msg_o", req="req_o")],
                mtime_ms=ms(8, 1, 5))
    os.symlink(str(folder / "-proj-a"), str(folder / "-Volumes-Work-proj-a"))
    return folder
