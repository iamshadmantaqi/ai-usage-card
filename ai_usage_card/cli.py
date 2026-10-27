"""Entry point: read Claude Code's transcripts and T3 Code's caches and the saved daily history, build the card, write it or publish it.

Exit 0 when the card is current (published or unchanged); exit 1 when it refuses, with the
reason in the log. A refusal never commits or pushes anything.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import history as ledger_file
from . import publish as publisher
from .claude import PROJECTS_DIR, load_claude_records
from .load import CacheError, UsageRecord, load_t3
from .pricing import load_rates
from .privacy import PrivacyError, check_privacy
from .history import Ledger, LedgerError
from .render import DARK, LIGHT, fmt_cost, fmt_tokens, period_label, render, render_phone
from .summarise import TIMEZONE, freeze, last_frozen_day, local_day, summarise, window_start_ms

HOME = Path.home()
DEFAULT_USERDATA = HOME / ".t3" / "userdata"
DEFAULT_CLONE = HOME / "Library" / "Application Support" / "ai-usage-card" / "profile"
DEFAULT_HISTORY = HOME / "Library" / "Application Support" / "ai-usage-card" / "history.json"
DEFAULT_LOG = HOME / "Library" / "Logs" / "ai-usage-card.log"
DEFAULT_REPO = "https://github.com/iamshadmantaqi/iamshadmantaqi.git"
GH_CREDENTIAL_HELPER = "!/opt/homebrew/bin/gh auth git-credential"
MAX_RECORD_AGE = timedelta(hours=48)  # no record at all from any source this recent means nothing is being logged
FILE_SLACK_MS = 36 * 3600 * 1000  # T3 reads transcripts modified up to 36 hours before the window

log = logging.getLogger("ai-usage-card")


class Refused(Exception):
    """The data is not fit to publish."""


def gather(userdata: Path, projects: Path, now: datetime) -> Tuple[List[UsageRecord], Optional[int]]:
    """Records for the window, and the time both of T3 Code's caches are complete up to (None if they cover
    nothing): the older of the scan cache's newest record and Cursor's `untilMs`, so one fresh cache cannot hide
    the other being stale.

    Claude Code comes from its own transcripts (see `claude.py`), read from `projects` and from every
    Claude folder T3 scans; T3's Claude records are dropped so nothing is counted twice. Everything
    else stays on T3's caches. Refuses when T3 has Claude usage in the window and the transcripts give
    none, which means Claude Code's data has moved out of reach."""
    now_ms = int(now.timestamp() * 1000)
    floor_ms = window_start_ms(now.astimezone(TIMEZONE).date()) - FILE_SLACK_MS
    # A timestamp in the future (clock skew, a bad row) would defeat the age check in build_cards.
    in_range = lambda r: floor_ms <= r.timestamp_ms <= now_ms
    t3_records, t3_claude_folders, cursor_until = load_t3(userdata, floor_ms)
    t3 = [r for r in t3_records if in_range(r)]
    scan_newest = max((r.timestamp_ms for r in t3 if r.provider != "cursor"), default=None)
    t3_newest = scan_newest if cursor_until is None or scan_newest is None else min(scan_newest, cursor_until)
    claude = [r for r in load_claude_records([projects] + [Path(f) for f in t3_claude_folders], floor_ms) if in_range(r)]
    if not claude and any(r.provider == "claude" for r in t3):
        raise Refused("T3 Code has Claude Code usage in the window but its transcripts gave none")
    return [r for r in t3 if r.provider != "claude"] + claude, t3_newest


def build_cards(userdata: Path, now: datetime, projects: Path = PROJECTS_DIR, ledger: Optional[Ledger] = None) -> Dict[str, str]:
    """The four SVGs, without the history that was frozen on the way (see `build`)."""
    return build(userdata, now, projects, ledger)[0]


def build(userdata: Path, now: datetime, projects: Path = PROJECTS_DIR, ledger: Optional[Ledger] = None) -> Tuple[Dict[str, str], Ledger]:
    """The four SVGs (wide and phone, light and dark) and the history with any newly frozen days, or
    Refused / CacheError / PrivacyError. Pure of side effects except reading: the caller saves the history.

    The window is six months and ends today; days 30 or more days old come from `ledger` once frozen. Claude Code is read from its transcripts, so it is always current; T3
    Code saves its usage cache only when its usage page is open, so a stale T3 cache costs the
    other tools their recent days, which is logged as a warning."""
    now_ms = int(now.timestamp() * 1000)
    max_age_ms = MAX_RECORD_AGE.total_seconds() * 1000
    records, t3_newest = gather(userdata, projects, now)
    if not records:
        raise Refused("no usage records found")
    newest_ms = max(r.timestamp_ms for r in records)
    if now_ms - newest_ms > max_age_ms:
        raise Refused("newest record is %.0f hours old (limit %d): no source has recent usage"
                      % ((now_ms - newest_ms) / 3.6e6, MAX_RECORD_AGE.total_seconds() / 3600))
    if t3_newest is None or now_ms - t3_newest > max_age_ms:
        log.warning("T3 Code's caches are %s: Cursor, Codex and other tools may be missing recent days"
                    " (open its usage page)", "missing" if t3_newest is None else "%.0f hours old" % ((now_ms - t3_newest) / 3.6e6))
    ledger = ledger or Ledger.empty()
    rates, today = load_rates(userdata), now.astimezone(TIMEZONE).date()
    # A day is frozen once T3 Code's caches reach past it (they are saved when its usage page is open), or once it
    # is old enough that Claude Code's cleanup would otherwise take its transcripts first.
    through = local_day(t3_newest) - timedelta(days=1) if t3_newest is not None else None
    if through is None or through < last_frozen_day(today, through):
        log.warning("T3 Code's caches end %s: days from %s on are frozen without waiting for them",
                    "nowhere in the window" if through is None else through.isoformat(), last_frozen_day(today, through).isoformat())
    frozen = freeze(records, rates, today, ledger, through)
    summary = summarise(records, rates, today, newest_ms, frozen)
    cards = {"ai-usage-light.svg": render(summary, LIGHT), "ai-usage-dark.svg": render(summary, DARK),
             "ai-usage-phone-light.svg": render_phone(summary, LIGHT), "ai-usage-phone-dark.svg": render_phone(summary, DARK)}
    for svg in cards.values():
        check_privacy(svg)
    log.info("%s: %s tokens, %s API-equivalent, %d weeks, updated %s; history holds %d days (%d frozen this run)",
             period_label(summary).lower(), fmt_tokens(summary.total_tokens), fmt_cost(summary.total_cost_usd),
             len(summary.weekly), summary.updated.isoformat(), len(frozen.days), len(frozen.days) - len(ledger.days))
    return cards, frozen


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_usage_card", description=__doc__)
    parser.add_argument("--userdata", type=Path, default=DEFAULT_USERDATA, help="T3 Code's userdata folder")
    parser.add_argument("--claude-projects", type=Path, default=PROJECTS_DIR, help="Claude Code's transcripts folder (the Claude folders T3 Code scans are read as well)")
    parser.add_argument("--out", type=Path, help="write the SVGs here and do not publish (the history is then only read, unless --history is given)")
    parser.add_argument("--history", type=Path, help="the saved daily history (default: %s)" % DEFAULT_HISTORY)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="profile repo to publish to")
    parser.add_argument("--clone", type=Path, default=DEFAULT_CLONE, help="working clone used for publishing")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--git-credential-helper", default=GH_CREDENTIAL_HELPER,
                        help="credential helper for the working clone ('' to use the system one)")
    args = parser.parse_args(argv)

    args.log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(str(args.log))] + ([logging.StreamHandler(sys.stderr)] if sys.stderr.isatty() else []),
                        force=True)
    try:
        history_path = args.history or DEFAULT_HISTORY
        saved = ledger_file.load(history_path)  # a corrupt file refuses the run here, before anything can overwrite it
        cards, frozen = build(args.userdata, now or datetime.now(TIMEZONE), args.claude_projects, saved)
        added = len(frozen.days) - len(saved.days)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            for name, svg in cards.items():
                (args.out / name).write_text(svg, encoding="utf-8")
            log.info("wrote %d files to %s", len(cards), args.out)
        elif publisher.publish(cards, args.repo, args.clone, args.git_credential_helper or None):
            log.info("published a new card to branch %s", publisher.BRANCH)
        else:
            log.info("card unchanged, nothing committed")
        # Saved after the card is out, so a history that cannot be written does not hold the card back; the days
        # it would have added are still live, and the next run saves them.
        if frozen != saved and args.out and not args.history:
            log.info("trial run: %d new days not saved to %s (pass --history to save them)", added, history_path)
        elif frozen != saved:
            try:
                ledger_file.save(history_path, frozen)
            except OSError as error:
                raise LedgerError("history file %s cannot be written (%s: %s)" % (history_path, type(error).__name__, error))
            log.info("saved %d new days to %s", added, history_path)
        return 0
    except (Refused, CacheError, PrivacyError, LedgerError, publisher.PublishError) as error:
        log.error("refused: %s: %s", type(error).__name__, error)
        return 1
    except Exception:
        log.exception("unexpected failure")
        return 1
