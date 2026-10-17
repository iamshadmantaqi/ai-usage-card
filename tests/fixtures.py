"""A small, hand-made T3 Code userdata folder. Every expected number in the tests is worked out
by hand from this data (see the comments), not computed by the code under test."""
from __future__ import annotations

import json
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


def scan_cache():
    return {
        "sources": {}, "version": 5, "models": MODELS, "sessions": SESSIONS,
        "files": {
            "/f/a.jsonl": entry("claude", [A1, A2, A3, A4, A5]),
            "/f/b.jsonl": entry("claude", [A1, B1]),  # a fork that copied A1 forward
            "/f/c.jsonl": entry("codex", [CODEX]),
            "/f/d.jsonl": entry("codex", [CODEX]),  # the same rollout read from a second file
            "/f/e.jsonl": entry("grok", [E1] + E2),
        },
    }


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
                            "provider_specific_entry": {"fast": 2}},
        "claude-sonnet-5-5": {"input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5,
                              "cache_read_input_token_cost": 1e-7, "cache_creation_input_token_cost": 2.5e-6},
        "openai/gpt-6-luna": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7},
        "xai/grok-4.6": {"input_cost_per_token": 2e-6, "output_cost_per_token": 6e-6},
    }}


def write_userdata(folder: Path, scan=None, cursor=None, rate_table=None, cursor_enabled=True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"cursorKeychainUsageEnabled": cursor_enabled}))
    (folder / "usage-scan-cache-v5.json").write_text(json.dumps(scan if scan is not None else scan_cache()))
    (folder / "usage-cursor-account-cache-v1.json").write_text(json.dumps(cursor if cursor is not None else cursor_cache()))
    (folder / "usage-model-rates.json").write_text(json.dumps(rate_table if rate_table is not None else rates()))
    return folder
