"""The saved daily history and the six-month window. Every expected value is written out by hand."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from ai_usage_card import cli, history
from ai_usage_card.history import Ledger, LedgerError, Row
from ai_usage_card.load import Tokens, UsageRecord
from ai_usage_card.render import DARK, LIGHT, render, render_phone
from ai_usage_card.summarise import TIMEZONE, cut_off_day, freeze, summarise
from tests import fixtures as fx

TODAY = date(2026, 10, 28)  # a Wednesday; today - 30 days is Mon 28 Sep, today - 29 days is Tue 29 Sep
OPUS = "claude-opus-5-5"


def at(month, day, hour, minute=0):
    """Epoch ms for a wall-clock time in Dhaka on 2026-month-day."""
    return int(datetime(2026, month, day, hour, minute, tzinfo=TIMEZONE).timestamp() * 1000)


def rec(when, tokens, cost, provider="claude", model=OPUS):
    """A live record: `when` is epoch ms, `tokens` all uncached input, `cost` the provider-reported dollars (no rate table needed)."""
    return UsageRecord(provider, when, model, None, "s", Tokens(tokens, 0, 0, 0, 0), cost, "standard", None)


def row(tool, model, tokens, cost):
    return Row(tool, model, tokens, cost)


def ledger_of(**days):
    """ledger_of(d0928=[row(...)]) keyed 'dMMDD' for brevity."""
    return Ledger({date(2026, int(k[1:3]), int(k[3:5])): rows for k, rows in days.items()})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class FreezeTests(Base):
    def records(self):
        return [
            rec(at(8, 31, 1, 0), 10, 0.1),                  # first record 01:00: a full first day
            rec(at(9, 10, 10), 100, 1.5),
            rec(at(9, 10, 14), 50, 0.5),                     # same day, same tool and model: one row
            rec(at(9, 10, 15), 7, 0.25, "codex", "gpt-6-luna"),
            rec(at(9, 28, 9), 40, 0.4),                      # exactly 30 days old: frozen
            rec(at(9, 29, 9), 30, 0.3),                      # 29 days old: stays live
            rec(at(10, 28, 1), 5, 0.05),
        ]

    def test_days_at_least_30_old_are_frozen_with_one_row_per_tool_and_model(self):
        frozen = freeze(self.records(), {}, TODAY, Ledger.empty(), through=date(2026, 10, 27))
        self.assertEqual(sorted(frozen.days), [date(2026, 8, 31), date(2026, 9, 10), date(2026, 9, 28)])
        self.assertEqual(frozen.days[date(2026, 9, 10)], [row("claude", OPUS, 150, 2.0), row("codex", "gpt-6-luna", 7, 0.25)])
        self.assertEqual(frozen.days[date(2026, 9, 28)], [row("claude", OPUS, 40, 0.4)])

    def test_a_saved_day_is_never_changed_and_freezing_twice_gives_the_same_ledger(self):
        saved = ledger_of(d0910=[row("claude", OPUS, 1, 0.01)])
        once = freeze(self.records(), {}, TODAY, saved, through=date(2026, 10, 27))
        self.assertEqual(once.days[date(2026, 9, 10)], [row("claude", OPUS, 1, 0.01)])  # live says 150, the saved 1 stays
        self.assertEqual(sorted(once.days), [date(2026, 8, 31), date(2026, 9, 10), date(2026, 9, 28)])
        self.assertEqual(freeze(self.records(), {}, TODAY, once, through=date(2026, 10, 27)), once)
        self.assertIs(freeze(self.records(), {}, TODAY, once, through=date(2026, 10, 27)), once)

    def test_nothing_is_frozen_past_the_day_t3_codes_cache_reaches_or_without_it(self):
        records = self.records()
        self.assertEqual(sorted(freeze(records, {}, TODAY, Ledger.empty(), through=date(2026, 9, 10)).days),
                         [date(2026, 8, 31), date(2026, 9, 10)])
        self.assertEqual(freeze(records, {}, TODAY, Ledger.empty(), through=None), Ledger.empty())

    def test_names_that_are_not_plain_are_frozen_as_other(self):
        records = [rec(at(8, 31, 1), 1, 0.0), rec(at(9, 1, 9), 3, 0.0, "/Users/x/tool", "stealth/ox-alpha"),
                   rec(at(9, 1, 9), 4, 0.0, "claude", "<synthetic>")]
        frozen = freeze(records, {}, TODAY, Ledger.empty(), through=TODAY)
        self.assertEqual(frozen.days[date(2026, 9, 1)], [row("other", "other", 3, 0.0)])
        self.assertNotIn("Users", history.dump(frozen))

    def test_cursors_default_model_is_frozen_under_its_display_name(self):
        frozen = freeze([rec(at(8, 31, 1), 1, 0.0), rec(at(9, 1, 9), 3, 0.5, "cursor", "default")], {}, TODAY, Ledger.empty(), through=TODAY)
        self.assertEqual(frozen.days[date(2026, 9, 1)], [row("cursor", "cursor-auto", 3, 0.5)])


class PartialFirstDayTests(Base):
    def test_the_oldest_day_is_skipped_when_its_first_record_is_at_or_after_0200(self):
        records = [rec(at(8, 28, 15), 999, 9.0), rec(at(8, 29, 9), 20, 0.2), rec(at(9, 1, 9), 30, 0.3)]
        self.assertEqual(cut_off_day(records, Ledger.empty(), TODAY), date(2026, 8, 28))
        frozen = freeze(records, {}, TODAY, Ledger.empty(), through=TODAY)
        self.assertEqual(sorted(frozen.days), [date(2026, 8, 29), date(2026, 9, 1)])
        s = summarise(records, {}, TODAY, ledger=frozen)
        self.assertEqual((s.first_day, s.total_tokens), (date(2026, 8, 29), 50))  # not 1049

    def test_the_first_full_day_is_the_day_after_the_cut_off_one_even_with_no_usage_on_it(self):
        records = [rec(at(8, 28, 15), 999, 9.0), rec(at(9, 1, 9), 30, 0.3)]
        s = summarise(records, {}, TODAY)
        self.assertEqual((s.first_day, s.total_tokens, s.daily[0].tokens), (date(2026, 8, 29), 30, 0))

    def test_a_first_record_before_0200_is_a_full_day(self):
        records = [rec(at(8, 28, 1, 59), 11, 0.1), rec(at(9, 1, 9), 30, 0.3)]
        self.assertIsNone(cut_off_day(records, Ledger.empty(), TODAY))
        self.assertEqual(summarise(records, {}, TODAY).first_day, date(2026, 8, 28))
        self.assertEqual(cut_off_day([rec(at(8, 28, 2, 0), 11, 0.1)], Ledger.empty(), TODAY), date(2026, 8, 28))

    def test_a_young_oldest_day_is_a_real_start_and_a_day_the_history_has_is_never_cut_off(self):
        young = [rec(at(10, 20, 15), 5, 0.1)]
        self.assertIsNone(cut_off_day(young, Ledger.empty(), TODAY))
        self.assertEqual(summarise(young, {}, TODAY).first_day, date(2026, 10, 20))
        old = [rec(at(8, 28, 15), 5, 0.1)]
        self.assertIsNone(cut_off_day(old, ledger_of(d0828=[row("claude", OPUS, 5, 0.1)]), TODAY))
        self.assertIsNone(cut_off_day(old, ledger_of(d0827=[row("claude", OPUS, 5, 0.1)]), TODAY))


class LongHistoryToolTests(Base):
    def test_cursor_days_before_the_file_based_tools_start_do_not_move_the_start(self):
        records = [rec(at(8, 17, 10), 999, 9.0, "cursor", "cursor-auto"),   # Cursor's server keeps more than the 60-day files
                   rec(at(8, 27, 15), 888, 8.0),                              # Claude Code's oldest day: cut off
                   rec(at(8, 28, 9), 20, 0.2), rec(at(8, 28, 10), 5, 0.05, "cursor", "cursor-auto")]
        self.assertEqual(cut_off_day(records, Ledger.empty(), TODAY), date(2026, 8, 27))
        frozen = freeze(records, {}, TODAY, Ledger.empty(), through=TODAY)
        self.assertEqual(sorted(frozen.days), [date(2026, 8, 28)])
        s = summarise(records, {}, TODAY, ledger=frozen)
        self.assertEqual((s.first_day, s.total_tokens, [(t.label, t.tokens) for t in s.tools]),
                         (date(2026, 8, 28), 25, [("Claude Code", 20), ("Cursor", 5)]))

    def test_with_only_cursor_data_it_sets_the_start_itself(self):
        records = [rec(at(8, 17, 1), 7, 0.1, "cursor", "cursor-auto")]
        self.assertEqual(summarise(records, {}, TODAY).first_day, date(2026, 8, 17))


class WindowTests(Base):
    def test_a_day_is_counted_from_exactly_one_source_at_the_30_day_boundary(self):
        saved = ledger_of(
            d0920=[row("claude", OPUS, 11, 0.11)],
            d0928=[row("claude", OPUS, 5, 0.05)],    # 30 days old: the saved 5, not the live 100
            d0929=[row("claude", OPUS, 7, 0.07)],    # 29 days old: a stray entry, ignored; the live 30 counts
        )
        records = [rec(at(9, 28, 9), 100, 1.0), rec(at(9, 29, 9), 30, 0.3)]
        s = summarise(records, {}, TODAY, ledger=saved)
        self.assertEqual({d.day: d.tokens for d in s.daily if d.tokens}, {date(2026, 9, 20): 11, date(2026, 9, 28): 5, date(2026, 9, 29): 30})
        self.assertEqual(s.total_tokens, 46)
        self.assertAlmostEqual(s.total_cost_usd, 0.11 + 0.05 + 0.3, places=9)
        self.assertEqual(s.models[0].tokens, 46)

    def test_freezing_a_day_does_not_change_the_totals(self):
        records = [rec(at(8, 31, 1), 10, 0.1), rec(at(9, 28, 9), 100, 1.0), rec(at(9, 29, 9), 30, 0.3)]
        live = summarise(records, {}, TODAY)
        frozen = summarise(records, {}, TODAY, ledger=freeze(records, {}, TODAY, Ledger.empty(), through=TODAY))
        self.assertEqual((live.total_tokens, frozen.total_tokens), (140, 140))
        self.assertEqual([w.tokens for w in live.weekly], [w.tokens for w in frozen.weekly])

    def test_totals_tools_and_models_cover_ledger_and_live_days_together(self):
        saved = ledger_of(d0601=[row("claude", OPUS, 1000, 10.0), row("codex", "gpt-6-luna", 500, 1.0)])
        records = [rec(at(10, 27, 9), 200, 2.0), rec(at(10, 27, 10), 300, 3.0, "cursor", "cursor-auto")]
        s = summarise(records, {}, TODAY, ledger=saved)
        self.assertEqual(s.total_tokens, 2000)
        self.assertAlmostEqual(s.total_cost_usd, 16.0)
        self.assertEqual([(t.label, t.tokens) for t in s.tools], [("Claude Code", 1200), ("Codex", 500), ("Cursor", 300)])
        self.assertEqual([(m.label, m.tokens) for m in s.models], [(OPUS, 1200), ("gpt-6-luna", 500), ("cursor-auto", 300)])

    def test_the_label_is_since_the_first_day_until_the_history_reaches_back_six_months(self):
        saved = ledger_of(d0828=[row("claude", OPUS, 1000, 10.0)])
        s = summarise([], {}, TODAY, ledger=saved)
        self.assertEqual((s.full_window, s.first_day), (False, date(2026, 8, 28)))
        for draw in (render, render_phone):
            for theme in (LIGHT, DARK):
                svg = draw(s, theme)
                ET.fromstring(svg)
                self.assertIn(">AI USAGE · SINCE 28 AUG 2026<", svg)
                self.assertNotIn("LAST 6 MONTHS", svg)
                self.assertIn('aria-label="AI usage since 28 Aug 2026: 1.0K tokens, $10.00 API-equivalent"', svg)

    def test_the_label_switches_to_last_six_months_on_the_day_the_history_covers_182_days(self):
        # the 182-day window ending 28 Oct 2026 starts 30 Apr 2026
        just_short = summarise([], {}, TODAY, ledger=ledger_of(d0501=[row("claude", OPUS, 1, 0.0)]))
        self.assertEqual((just_short.full_window, just_short.first_day), (False, date(2026, 5, 1)))
        full = summarise([], {}, TODAY, ledger=ledger_of(d0430=[row("claude", OPUS, 1, 0.0)], d0429=[row("claude", OPUS, 90, 0.0)]))
        self.assertEqual((full.full_window, full.first_day, len(full.daily), full.total_tokens), (True, date(2026, 4, 30), 182, 1))  # 29 Apr is out
        for draw in (render, render_phone):
            for theme in (LIGHT, DARK):
                svg = draw(full, theme)
                self.assertIn(">AI USAGE · LAST 6 MONTHS<", svg)
                self.assertNotIn("SINCE", svg)
                self.assertIn('aria-label="AI usage over the last 6 months: 1 tokens, $0 API-equivalent"', svg)

    def test_a_history_older_than_the_window_rolls_off_its_oldest_days(self):
        s = summarise([], {}, date(2026, 11, 1), ledger=ledger_of(d0430=[row("claude", OPUS, 8, 0.0)], d0501=[row("claude", OPUS, 9, 0.0)]))
        self.assertEqual((s.first_day, s.total_tokens, s.full_window), (date(2026, 5, 4), 0, True))


class WeeklyChartTests(Base):
    def summary(self):
        records = [
            rec(at(10, 11, 12), 10, 0.1),       # Sun 11 Oct
            rec(at(10, 12, 0, 30), 20, 0.2),    # Mon 12 Oct, half past midnight in Dhaka (still Sunday in UTC): this week
            rec(at(10, 18, 23, 30), 30, 0.3),   # Sun 18 Oct, 11:30 pm Dhaka: still the week of 12 Oct
            rec(at(10, 19, 0, 30), 40, 0.4),    # Mon 19 Oct 00:30 Dhaka (Sunday 18:30 UTC): the next week
            rec(at(10, 27, 9), 60, 0.6),
            rec(at(10, 28, 9), 70, 0.7),
        ]
        return summarise(records, {}, TODAY)

    def test_weeks_start_on_monday_in_dhaka_and_the_partial_ends_are_clipped_to_the_window(self):
        weeks = [(w.first, w.last, w.tokens, round(w.cost_usd, 9), w.current) for w in self.summary().weekly]
        self.assertEqual(weeks, [
            (date(2026, 10, 11), date(2026, 10, 11), 10, 0.1, False),   # the window starts on a Sunday: a one-day week
            (date(2026, 10, 12), date(2026, 10, 18), 50, 0.5, False),
            (date(2026, 10, 19), date(2026, 10, 25), 40, 0.4, False),
            (date(2026, 10, 26), date(2026, 10, 28), 130, 1.3, True),   # this week, Mon to today (Wed)
        ])

    def test_the_weeks_add_up_to_the_days_and_the_total(self):
        s = self.summary()
        self.assertEqual((sum(w.tokens for w in s.weekly), sum(d.tokens for d in s.daily), s.total_tokens), (230, 230, 230))

    def test_a_full_window_has_27_bars_and_an_empty_week_is_a_stub(self):
        s = summarise([rec(at(10, 28, 9), 5, 0.0)], {}, TODAY, ledger=ledger_of(d0430=[row("claude", OPUS, 9, 0.0)]))
        self.assertEqual((len(s.weekly), s.weekly[0].first, s.weekly[-1].last), (27, date(2026, 4, 30), TODAY))  # Thu 30 Apr to Wed 28 Oct
        svg = render(s, DARK)
        self.assertEqual(svg.count('height="1.5"'), 25)  # 27 weeks, two with usage

    def test_only_the_current_week_is_drawn_muted_with_a_title_that_says_so_far(self):
        for draw in (render, render_phone):
            svg = draw(self.summary(), DARK)
            self.assertEqual(len(re.findall(r'fill-opacity="0.4"', svg)), 1)
            self.assertIn("<title>26 Oct to 28 Oct (so far): 130, $1.30</title>", svg)
            self.assertIn("<title>12 Oct to 18 Oct: 50, $0.50</title>", svg)
            self.assertIn("<title>11 Oct: 10, $0.10</title>", svg)
            self.assertIn("TOKENS PER WEEK", svg)
            self.assertNotIn("TOKENS PER DAY", svg)
            self.assertIn("peak 130", svg)

    def test_both_layouts_keep_their_size(self):
        s = self.summary()
        self.assertEqual((ET.fromstring(render(s, DARK)).get("width"), ET.fromstring(render(s, DARK)).get("height")), ("840", "392"))
        self.assertEqual(ET.fromstring(render_phone(s, LIGHT)).get("width"), "400")


class LedgerFileTests(Base):
    def test_a_ledger_round_trips_and_is_written_by_rename(self):
        path = self.tmp / "sub" / "history.json"
        saved = ledger_of(d0910=[row("codex", "gpt-6-luna", 7, 0.25), row("claude", OPUS, 150, 2.0)])
        history.save(path, saved)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "days": {"2026-09-10": [
            {"tool": "claude", "model": OPUS, "tokens": 150, "cost_usd": 2.0},
            {"tool": "codex", "model": "gpt-6-luna", "tokens": 7, "cost_usd": 0.25}]}})
        self.assertEqual(history.load(path).days, {date(2026, 9, 10): [row("claude", OPUS, 150, 2.0), row("codex", "gpt-6-luna", 7, 0.25)]})
        self.assertEqual([p.name for p in path.parent.iterdir()], ["history.json"])  # no temporary file left

    def test_a_failed_write_leaves_the_old_file_whole_and_no_temporary_file(self):
        path = self.tmp / "history.json"
        history.save(path, ledger_of(d0910=[row("claude", OPUS, 1, 0.0)]))
        before = path.read_bytes()
        with mock.patch.object(history.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                history.save(path, ledger_of(d0910=[row("claude", OPUS, 2, 0.0)]))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["history.json"])

    def test_a_missing_file_is_an_empty_ledger(self):
        self.assertEqual(history.load(self.tmp / "nope.json"), Ledger.empty())

    def test_every_kind_of_damage_is_refused(self):
        row_ok = {"tool": "claude", "model": OPUS, "tokens": 1, "cost_usd": 0.5}
        bad = {
            "not json": "{nope",
            "empty file": "",
            "a list": "[]",
            "wrong version": json.dumps({"version": 2, "days": {}}),
            "no days": json.dumps({"version": 1}),
            "extra key": json.dumps({"version": 1, "days": {}, "note": "x"}),
            "bad date": json.dumps({"version": 1, "days": {"28/08/2026": [row_ok]}}),
            "compact date": json.dumps({"version": 1, "days": {"20260828": [row_ok]}}),
            "rows not a list": json.dumps({"version": 1, "days": {"2026-08-28": row_ok}}),
            "missing field": json.dumps({"version": 1, "days": {"2026-08-28": [{"tool": "claude", "model": OPUS, "tokens": 1}]}}),
            "extra field": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, path="/x")]}}),
            "negative tokens": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, tokens=-1)]}}),
            "fractional tokens": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, tokens=1.5)]}}),
            "boolean tokens": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, tokens=True)]}}),
            "string cost": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, cost_usd="1")]}}),
            "a path as the model": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, model="/Users/x/secret")]}}),
            "an id as the tool": json.dumps({"version": 1, "days": {"2026-08-28": [dict(row_ok, tool="msg_abc")]}}),
        }
        for name, text in bad.items():
            with self.subTest(name):
                path = self.tmp / "history.json"
                path.write_text(text)
                with self.assertRaises(LedgerError):
                    history.load(path)
        (self.tmp / "history.json").write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(LedgerError):
            history.load(self.tmp / "history.json")
        with self.assertRaises(LedgerError):
            history.load(self.tmp)  # a folder where the file should be


class CliHistoryTests(Base):
    def setUp(self):
        super().setUp()
        self.userdata = fx.write_userdata(self.tmp / "userdata")
        self.projects = fx.write_projects(self.tmp / "projects")
        # Sep 4, 10:00 Dhaka: 100 + 20 tokens, 100 * 1e-6 + 20 * 5e-6 = $0.0002. The 1 Aug transcript is the cut-off oldest day.
        fx.write_lines(self.projects / "-proj-a" / "chat9.jsonl", [fx.reply(OPUS, (9, 4, 4), fx.usage(100, 20), msg="msg_h", req="req_h")])
        self.history = self.tmp / "state" / "history.json"
        self.out = self.tmp / "out"
        patch = mock.patch.object(cli, "DEFAULT_HISTORY", self.tmp / "default-history.json")
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *extra, now=fx.NOW):
        args = ["--userdata", str(self.userdata), "--claude-projects", str(self.projects), "--log", str(self.tmp / "card.log")] + list(extra)
        return cli.main(args, now=now)

    def log(self):
        return (self.tmp / "card.log").read_text()

    def test_a_first_run_backfills_and_a_second_run_changes_nothing(self):
        self.assertFalse(self.history.exists())  # a missing ledger starts fresh
        self.assertEqual(self.run_cli("--out", str(self.out), "--history", str(self.history)), 0)
        first = self.history.read_bytes()
        self.assertEqual(json.loads(first), {"version": 1, "days": {"2026-09-04": [
            {"tool": "claude", "model": OPUS, "tokens": 120, "cost_usd": 0.0002}]}})  # 1 Aug is cut off, 1 Sep is T3's Claude copy (ignored)
        self.assertIn("saved 1 new days", self.log())
        cards = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.assertEqual(self.run_cli("--out", str(self.out), "--history", str(self.history)), 0)
        self.assertEqual(self.history.read_bytes(), first)
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()}, cards)
        self.assertEqual([p.name for p in self.history.parent.iterdir()], ["history.json"])
        self.assertIn("AI USAGE · SINCE 2 AUG 2026", cards["ai-usage-dark.svg"].decode())  # the day after the cut-off 1 Aug

    def test_the_log_line_names_the_period_weeks_and_history(self):
        self.run_cli("--out", str(self.out), "--history", str(self.history))
        self.assertRegex(self.log(), r"since 2 aug 2026: 11\.5K tokens, \$0\.03 API-equivalent, 12 weeks, updated 2026-10-18; "
                                     r"history holds 1 days \(1 frozen this run\)")

    def test_a_trial_run_with_out_does_not_save_unless_history_is_given(self):
        self.assertEqual(self.run_cli("--out", str(self.out)), 0)
        self.assertFalse((self.tmp / "default-history.json").exists())
        self.assertIn("trial run: 1 new days not saved", self.log())

    def test_a_corrupt_history_refuses_the_run_and_is_not_overwritten(self):
        self.history.parent.mkdir(parents=True)
        for text in ("{corrupt", json.dumps({"version": 1, "days": {"2026-09-04": [{"tool": "claude", "model": "/Users/x", "tokens": 1, "cost_usd": 0}]}})):
            self.history.write_text(text)
            self.assertEqual(self.run_cli("--out", str(self.out), "--history", str(self.history)), 1)
            self.assertEqual(self.history.read_text(), text)
            self.assertFalse(self.out.exists())
            self.assertEqual([p.name for p in self.history.parent.iterdir()], ["history.json"])
            self.assertIn("refused: LedgerError", self.log())

    def test_a_saved_day_wins_over_the_live_data_for_the_same_day(self):
        saved = ledger_of(d0904=[row("claude", OPUS, 5, 0.5)])
        history.save(self.history, saved)
        self.assertEqual(self.run_cli("--out", str(self.out), "--history", str(self.history)), 0)
        self.assertEqual(history.load(self.history), saved)  # live says 120 tokens; the frozen 5 stays
        # T3's other tools 5856 + Claude Code 5570 + the saved 5 = 11431 tokens; $0.5 + 0.032005 (without the 0.0002 for the live 120)
        self.assertIn('aria-label="AI usage since 2 Aug 2026: 11.4K tokens, $0.53 API-equivalent"', (self.out / "ai-usage-dark.svg").read_text())


if __name__ == "__main__":
    unittest.main()
