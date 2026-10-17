"""Entry point: read T3 Code's caches, build the card, write it or publish it.

Exit 0 when the card is current (published or unchanged); exit 1 when it refuses, with the
reason in the log. A refusal never commits or pushes anything.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from . import publish as publisher
from .load import CacheError, load_records
from .pricing import load_rates
from .privacy import PrivacyError, check_privacy
from .render import DARK, LIGHT, fmt_cost, fmt_tokens, render
from .summarise import TIMEZONE, summarise, window_start_ms

HOME = Path.home()
DEFAULT_USERDATA = HOME / ".t3" / "userdata"
DEFAULT_CLONE = HOME / "Library" / "Application Support" / "ai-usage-card" / "profile"
DEFAULT_LOG = HOME / "Library" / "Logs" / "ai-usage-card.log"
DEFAULT_REPO = "https://github.com/iamshadmantaqi/iamshadmantaqi.git"
GH_CREDENTIAL_HELPER = "!/opt/homebrew/bin/gh auth git-credential"
MAX_RECORD_AGE = timedelta(hours=48)  # older means T3 Code has not been running
FILE_SLACK_MS = 36 * 3600 * 1000  # T3 reads transcripts modified up to 36 hours before the window
RETENTION_MS = 92 * 24 * 3600 * 1000  # T3 forgets files older than this

log = logging.getLogger("ai-usage-card")


class Refused(Exception):
    """The data is not fit to publish."""


def build_cards(userdata: Path, now: datetime) -> Dict[str, str]:
    """The two SVGs, or Refused / CacheError / PrivacyError. Pure of side effects except reading."""
    now_ms = int(now.timestamp() * 1000)
    today = now.astimezone(TIMEZONE).date()
    records = load_records(userdata, max(window_start_ms(today) - FILE_SLACK_MS, now_ms - RETENTION_MS))
    if not records:
        raise Refused("no usage records found")
    newest_ms = max(r.timestamp_ms for r in records)
    if now_ms - newest_ms > MAX_RECORD_AGE.total_seconds() * 1000:
        raise Refused("newest record is %.0f hours old (limit %d): T3 Code is not running"
                      % ((now_ms - newest_ms) / 3.6e6, MAX_RECORD_AGE.total_seconds() / 3600))
    summary = summarise(records, load_rates(userdata), today)
    cards = {"ai-usage-light.svg": render(summary, LIGHT), "ai-usage-dark.svg": render(summary, DARK)}
    for svg in cards.values():
        check_privacy(svg)
    log.info("30 days: %s tokens, %s API-equivalent, updated %s", fmt_tokens(summary.total_tokens),
             fmt_cost(summary.total_cost_usd), summary.updated.isoformat())
    return cards


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_usage_card", description=__doc__)
    parser.add_argument("--userdata", type=Path, default=DEFAULT_USERDATA, help="T3 Code's userdata folder")
    parser.add_argument("--out", type=Path, help="write the SVGs here and do not publish")
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
        cards = build_cards(args.userdata, now or datetime.now(TIMEZONE))
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            for name, svg in cards.items():
                (args.out / name).write_text(svg, encoding="utf-8")
            log.info("wrote %d files to %s", len(cards), args.out)
        elif publisher.publish(cards, args.repo, args.clone, args.git_credential_helper or None):
            log.info("published a new card to branch %s", publisher.BRANCH)
        else:
            log.info("card unchanged, nothing committed")
        return 0
    except (Refused, CacheError, PrivacyError, publisher.PublishError) as error:
        log.error("refused: %s: %s", type(error).__name__, error)
        return 1
    except Exception:
        log.exception("unexpected failure")
        return 1
