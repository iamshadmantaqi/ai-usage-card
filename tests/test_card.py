from __future__ import annotations

import errno
import os
import re
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from ai_usage_card import cli, history, publish
from ai_usage_card.claude import _timestamp_ms, load_claude_records, transcript_files
from ai_usage_card.load import CacheError, load_records
from ai_usage_card.pricing import load_rates, parse_rate_table, price
from ai_usage_card.privacy import PrivacyError, check_privacy
from ai_usage_card.render import DARK, LIGHT, fmt_cost, fmt_tokens, render, render_phone
from ai_usage_card.summarise import summarise
from tests import fixtures as fx

TODAY = date(2026, 10, 18)


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.userdata = fx.write_userdata(self.tmp / "userdata")
        self.projects = fx.write_projects(self.tmp / "projects")
        patch = mock.patch.object(cli, "DEFAULT_HISTORY", self.tmp / "default-history.json")  # no test may touch the real history
        patch.start()
        self.addCleanup(patch.stop)

    def summary(self):
        return summarise(load_records(self.userdata), load_rates(self.userdata), TODAY)


class LoadTests(Case):
    def test_duplicates_across_files_count_once(self):
        records = load_records(self.userdata)
        keys = [r.dedupe_key for r in records if r.dedupe_key]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sum(1 for r in records if r.provider == "codex"), 1)  # same rollout in two files

    def test_wrong_version_is_refused(self):
        scan = fx.scan_cache()
        scan["version"] = 4
        fx.write_userdata(self.userdata, scan=scan)
        with self.assertRaises(CacheError):
            load_records(self.userdata)

    def test_missing_and_unparsable_files_are_refused(self):
        (self.userdata / "usage-scan-cache-v5.json").write_text("{not json")
        with self.assertRaises(CacheError):
            load_records(self.userdata)
        (self.userdata / "usage-scan-cache-v5.json").unlink()
        with self.assertRaises(CacheError):
            load_records(self.userdata)

    def test_cursor_usage_is_left_out_when_t3_has_it_switched_off(self):
        fx.write_userdata(self.userdata, cursor_enabled=False)
        self.assertEqual([r for r in load_records(self.userdata) if r.provider == "cursor"], [])
        (self.userdata / "settings.json").unlink()  # T3's default is off
        self.assertEqual([r for r in load_records(self.userdata) if r.provider == "cursor"], [])

    def test_only_the_most_recently_fetched_cursor_account_counts(self):
        cursor = fx.cursor_cache()
        old = dict(cursor["accounts"]["keychain"], accountKey="old", fetchedAtMs=0,
                   records=[[fx.ms(10, 17, 3), 0, 1, 7, 0, 0, 0, None, "cursor-account:old:1:0"]])
        cursor["accounts"]["/old/auth.json"] = old
        fx.write_userdata(self.userdata, cursor=cursor)
        self.assertEqual([r.dedupe_key for r in load_records(self.userdata) if r.provider == "cursor"],
                         ["cursor-account:k:1:0"])

    def test_reads_a_version_9_cache_the_way_it_reads_version_5(self):
        v5 = sorted((r.provider, r.model, r.tokens.total) for r in load_records(self.userdata))
        v9 = fx.write_userdata(self.tmp / "userdata9", scan=fx.scan_cache_v9(), scan_version=9)
        self.assertEqual(sorted((r.provider, r.model, r.tokens.total) for r in load_records(v9)), v5)
        self.assertIn(("codex", "gpt-6-luna", 350), v5)

    def test_the_newest_supported_cache_file_is_the_one_read(self):
        # v5 still holds A1's 10000 tokens; the v9 file, written later by T3, has none of the Claude files.
        scan = fx.scan_cache_v9()
        scan["files"] = {k: v for k, v in scan["files"].items() if v["p"] != "claude"}
        fx.write_userdata(self.userdata, scan=scan, scan_version=9)
        self.assertEqual({r.provider for r in load_records(self.userdata)}, {"codex", "grok", "cursor"})

    def test_a_cache_version_newer_than_the_ones_checked_is_refused(self):
        for name in ("usage-scan-cache-v10.json", "usage-cursor-account-cache-v2.json"):
            (self.userdata / name).write_text("{}")
            with self.assertRaises(CacheError, msg=name) as caught:
                load_records(self.userdata)
            self.assertIn("T3 Code now writes " + name, str(caught.exception))
            (self.userdata / name).unlink()
        (self.userdata / "usage-scan-cache-v4.json").write_text("{}")  # an older one left behind is fine
        load_records(self.userdata)

    def test_a_file_whose_version_field_is_not_a_supported_one_is_refused(self):
        scan = fx.scan_cache_v9()
        scan["version"] = 10
        fx.write_userdata(self.tmp / "u10", scan=scan, scan_version=9)
        with self.assertRaises(CacheError):
            load_records(self.tmp / "u10")

    def test_a_file_whose_version_field_differs_from_its_name_is_refused(self):
        fx.write_userdata(self.tmp / "u9", scan=fx.scan_cache(version=5), scan_version=9)
        with self.assertRaisesRegex(CacheError, "unsupported scan cache version: 5"):
            load_records(self.tmp / "u9")

    def test_odd_cache_file_names_are_ignored_not_a_crash(self):
        for name in ("usage-scan-cache-v\u00b2.json", "usage-scan-cache-v010.json", "usage-scan-cache-vx.json"):
            (self.userdata / name).write_text("{}")
        self.assertTrue(load_records(self.userdata))

    def test_only_an_older_cache_file_says_the_current_one_is_missing(self):
        (self.userdata / "usage-scan-cache-v5.json").rename(self.userdata / "usage-scan-cache-v4.json")
        with self.assertRaises(CacheError) as caught:
            load_records(self.userdata)
        self.assertIn("cache file is missing: usage-scan-cache-v9.json", str(caught.exception))
        self.assertNotIn("now writes", str(caught.exception))

    def test_files_with_a_read_position_t3_rejects_are_skipped(self):
        scan = fx.scan_cache()
        scan["files"]["/f/e.jsonl"]["gl"] = 2000  # longer than the offset `o`
        fx.write_userdata(self.userdata, scan=scan)
        self.assertEqual([r for r in load_records(self.userdata) if r.provider == "grok"], [])

    def test_missing_cursor_file_means_no_cursor_records(self):
        (self.userdata / "usage-cursor-account-cache-v1.json").unlink()
        self.assertEqual([r for r in load_records(self.userdata) if r.provider == "cursor"], [])

    def test_cursor_default_model_is_shown_as_cursor_auto_at_the_same_cost(self):
        strings = ["default", "agent-session"]
        rows = [[fx.ms(10, 18, 3), 0, 1, 1000, 2000, 0, 500, None, "cursor-account:k:1:auto"]]
        cursor = {"version": 1, "strings": strings, "accounts": {"keychain": {
            "accountKey": "k", "sinceMs": 0, "untilMs": 1, "fetchedAtMs": 1, "records": rows}}}
        table = fx.rates()
        table["document"]["default"] = {"input_cost_per_token": 3e-6, "output_cost_per_token": 1e-5}
        fx.write_userdata(self.userdata, cursor=cursor, rate_table=table)
        rates = load_rates(self.userdata)
        record = next(r for r in load_records(self.userdata) if r.dedupe_key == "cursor-account:k:1:auto")
        self.assertEqual((record.model, record.rate_model), ("default", "default"))  # the record keeps the source name
        # priced through the original name: 3000 * 3e-6 + 500 * 1e-5
        self.assertAlmostEqual(price(rates, record), 0.014, places=9)
        labels = [m.label for m in self.summary().models]
        self.assertIn("cursor-auto", labels)
        self.assertNotIn("default", labels)


class ClaudeTranscriptTests(Case):
    def claude(self, min_file_mtime_ms=fx.ms(9, 1, 0)):
        return load_claude_records([self.projects], min_file_mtime_ms)

    def test_a_reply_written_three_times_counts_once_with_its_final_output(self):
        x = [r for r in self.claude() if r.tokens.output == 3000]
        self.assertEqual(len(x), 1)
        self.assertEqual(len([r for r in self.claude() if r.model == "claude-opus-5-5" and r.speed == "standard"]), 1)
        self.assertEqual(x[0].tokens.total, 5100)  # not 1110 (first copy) or 1200 (second)

    def test_subagent_files_count_and_a_symlinked_project_folder_does_not_double_count(self):
        records = self.claude()
        self.assertEqual(sorted(r.tokens.total for r in records), [170, 300, 5100])  # X, Y (subagent), F; synthetic, user and broken lines out

    def test_each_transcript_is_read_once_through_a_symlinked_folder_or_root(self):
        names = lambda *roots: sorted(os.path.relpath(p, os.path.realpath(str(self.projects))) for p in transcript_files(roots))
        expected = ["-proj-a/chat1.jsonl", "-proj-a/chat1/subagents/agent-1.jsonl", "-proj-a/chat2.jsonl", "-proj-old/old.jsonl"]
        self.assertEqual(names(self.projects), expected)
        os.symlink(str(self.projects), str(self.tmp / "alt"))  # like ~/.claude-alt/projects
        self.assertEqual(names(self.tmp / "alt"), expected)
        self.assertEqual(names(self.projects, self.tmp / "alt", self.tmp / "missing"), expected)  # T3 lists both

    def test_a_symlinked_transcript_file_and_a_symlink_cycle_are_read_once(self):
        os.symlink(str(self.projects / "-proj-a" / "chat2.jsonl"), str(self.projects / "-proj-old" / "linked.jsonl"))
        os.symlink(str(self.projects), str(self.projects / "-proj-a" / "loop"))
        os.symlink(str(self.tmp / "gone.jsonl"), str(self.projects / "-proj-old" / "broken.jsonl"))
        paths = list(transcript_files([self.projects]))
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(set(paths)), 4)

    def test_a_missing_or_unreadable_transcripts_folder_or_file_is_refused(self):
        with self.assertRaisesRegex(CacheError, "no Claude Code transcripts folder"):
            load_claude_records([self.tmp / "missing"])
        for target in (self.projects / "-proj-a" / "chat1" / "subagents", self.projects / "-proj-a" / "chat2.jsonl"):
            os.chmod(str(target), 0)
            self.addCleanup(os.chmod, str(target), 0o755)
            with self.assertRaisesRegex(CacheError, "cannot be read \\(PermissionError\\)$"):
                self.claude()  # EACCES is file permissions, so no Full Disk Access hint
            os.chmod(str(target), 0o755)

    def test_only_a_macos_privacy_block_gets_the_full_disk_access_hint(self):
        def blocked_walk(top, onerror=None, followlinks=False):
            onerror(PermissionError(errno.EPERM, "Operation not permitted"))  # what macOS privacy gives
            return iter(())
        with mock.patch("ai_usage_card.claude.os.walk", side_effect=blocked_walk):
            with self.assertRaisesRegex(CacheError, "Full Disk Access"):
                self.claude()

    def test_a_copy_without_a_request_id_is_still_the_same_reply(self):
        fx.write_lines(self.projects / "-proj-b" / "fork.jsonl", [
            fx.reply("claude-opus-5-5", (10, 17, 6), fx.usage(100, 3000, read=1000, five=400, hour=600), req=None)])
        self.assertEqual(sorted(r.tokens.total for r in self.claude()), [170, 300, 5100])

    def test_empty_ids_do_not_merge_different_replies(self):
        fx.write_lines(self.projects / "-proj-b" / "odd.jsonl", [
            fx.reply("claude-opus-5-5", (10, 17, 8), fx.usage(1, 1), msg="", req="req_p"),
            fx.reply("claude-opus-5-5", (10, 17, 8), fx.usage(2, 2), msg="", req="req_q"),
            fx.reply("claude-opus-5-5", (10, 17, 8), fx.usage(3, 3), msg="", req="")])
        self.assertEqual(sorted(r.tokens.total for r in self.claude()), [2, 4, 6, 170, 300, 5100])

    def test_claude_code_timestamps_are_read_with_any_fraction_length(self):
        self.assertEqual(_timestamp_ms("2026-10-17T06:00:00.123Z"), 1792216800123)
        self.assertEqual(_timestamp_ms("2026-10-17T06:00:00.123456Z"), 1792216800123)
        self.assertEqual(_timestamp_ms("2026-10-17T06:00:00.1234567Z"), 1792216800123)
        self.assertEqual(_timestamp_ms("2026-10-17T06:00:00.1Z"), 1792216800100)
        self.assertEqual(_timestamp_ms("2026-10-17T12:00:00+06:00"), 1792216800000)
        self.assertEqual(_timestamp_ms("2026-10-17T06:00:00"), 1792216800000)  # no zone is UTC
        for bad in ("2026-13-17T06:00:00Z", "yesterday", 1792216800123, None):
            self.assertIsNone(_timestamp_ms(bad))

    def test_files_last_written_before_the_window_are_skipped(self):
        self.assertEqual(len(self.claude()), 3)
        (self.projects / "-proj-old" / "old.jsonl").write_text(
            fx.reply("claude-opus-5-5", (8, 1, 5), fx.usage(1, 1), msg="msg_o", req="req_o") + "\n")
        self.assertEqual(len(self.claude(fx.ms(7, 1, 0))), 4)  # a fresh mtime brings it back

    def test_one_hour_writes_cost_more_than_five_minute_writes(self):
        rates = load_rates(self.userdata)
        by_total = {r.tokens.total: r for r in self.claude()}
        self.assertEqual(by_total[5100].tokens.cache_creation_1h, 600)
        self.assertAlmostEqual(price(rates, by_total[5100]), 0.0169, places=9)  # 600 at 2e-6; at 1.25e-6 it would be 0.01645
        self.assertAlmostEqual(price(rates, by_total[170]), 0.00055, places=9)  # unsplit writes are 5-minute
        self.assertAlmostEqual(price(rates, by_total[300]), 0.0022, places=9)  # fast mode doubles it

    def test_a_model_without_a_one_hour_rate_prices_those_writes_at_the_five_minute_rate(self):
        table = fx.rates()
        del table["document"]["claude-opus-5-5"]["cache_creation_input_token_cost_above_1hr"]
        fx.write_userdata(self.userdata, rate_table=table)
        record = next(r for r in self.claude() if r.tokens.total == 5100)
        self.assertAlmostEqual(price(load_rates(self.userdata), record), 0.01645, places=9)

    def test_nothing_from_a_transcript_but_numbers_a_model_and_a_time_reaches_the_records(self):
        for record in self.claude():
            self.assertEqual((record.provider, record.session_id, record.dedupe_key, record.reported_cost_usd), ("claude", "", None, None))
            self.assertNotIn("private", repr(record))
            self.assertNotIn("secret", repr(record))

    def test_the_claude_folders_t3_scans_are_read_too_without_double_counting(self):
        other = self.tmp / "other-account" / "projects"
        fx.write_lines(other / "-proj-z" / "chat.jsonl", [fx.reply("claude-opus-5-5", (10, 17, 9), fx.usage(30, 70), msg="msg_z", req="req_z")])
        scan = fx.scan_cache()
        scan["sources"] = {"claude\0" + str(self.tmp / "alt"): {"dir": str(self.projects)},
                           "claude\0" + str(other): {"dir": str(other)},
                           "codex\0" + str(self.tmp / "codex"): {"dir": str(self.tmp / "codex")}}
        fx.write_userdata(self.userdata, scan=scan)
        records, _ = cli.gather(self.userdata, self.projects, fx.NOW)
        # 2 is the 1 Aug transcript, inside the six-month floor now (summarise drops that cut-off day, see test_history)
        self.assertEqual(sorted(r.tokens.total for r in records if r.provider == "claude"), [2, 100, 170, 300, 5100])

    def test_refuses_when_t3_has_claude_usage_but_the_transcripts_give_none(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(cli.Refused, "transcripts gave none"):
            cli.gather(self.userdata, empty, fx.NOW)

    def test_a_stale_cursor_cache_holds_back_the_freeze_even_with_a_fresh_scan_cache(self):
        cursor = fx.cursor_cache()
        cursor["accounts"]["keychain"]["untilMs"] = fx.ms(9, 1, 0)  # Cursor's login lapsed on 1 Sep
        fx.write_userdata(self.userdata, cursor=cursor)
        self.assertEqual(cli.gather(self.userdata, self.projects, fx.NOW)[1], fx.ms(9, 1, 0))
        fx.write_userdata(self.userdata, cursor_enabled=False)  # Cursor off: the scan cache alone decides
        self.assertEqual(cli.gather(self.userdata, self.projects, fx.NOW)[1], fx.ms(10, 18, 2))

    def test_t3s_claude_records_are_ignored_but_its_other_tools_still_count(self):
        records, t3_newest = cli.gather(self.userdata, self.projects, fx.NOW)
        s = summarise(records, load_rates(self.userdata), TODAY)
        self.assertEqual([(t.label, t.tokens) for t in s.tools],
                         [("Claude Code", 5570), ("Cursor", 3500), ("Grok", 2006), ("Codex", 350)])
        self.assertEqual(s.total_tokens, 5570 + 3500 + 2006 + 350)
        # 0.0169 + 0.00055 + 0.0022 (claude) + 0.0123 (cursor, reported) + 0.000055 (codex) + 0 (grok, unpriced)
        self.assertAlmostEqual(s.total_cost_usd, 0.032005, places=6)
        self.assertEqual(t3_newest, fx.ms(10, 18, 2))  # the Codex row: the scan cache ends before Cursor's untilMs (03:00)


class PricingTests(Case):
    def test_a_priority_tier_without_a_one_hour_rate_keeps_the_standard_ratio(self):
        table = parse_rate_table({"m": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                                        "cache_creation_input_token_cost": 1.25e-6, "cache_creation_input_token_cost_above_1hr": 2e-6,
                                        "input_cost_per_token_priority": 2e-6, "output_cost_per_token_priority": 1e-5}})
        self.assertAlmostEqual(table["m"].fast.cache_creation, 2.5e-6, places=15)
        self.assertAlmostEqual(table["m"].fast.cache_creation_1h, 4e-6, places=15)  # not 2.5e-6, the 5-minute rate
        self.assertAlmostEqual(table["m"].standard.cache_creation_1h, 2e-6, places=15)


    def price_of(self, model):
        rates = load_rates(self.userdata)
        record = next(r for r in load_records(self.userdata) if r.model == model)
        return price(rates, record)

    def test_list_price_uses_all_four_token_kinds(self):
        self.assertAlmostEqual(self.price_of("claude-sonnet-5-5"), 0.007, places=9)

    def test_unpriced_model_costs_zero(self):
        self.assertEqual(self.price_of("mystery-model"), 0.0)

    def test_bare_alias_and_cache_fallback_to_input_rate(self):
        self.assertAlmostEqual(self.price_of("gpt-6-luna"), 0.000055, places=9)

    def test_variant_suffix_is_stripped_and_case_ignored(self):
        rates = load_rates(self.userdata)
        record = next(r for r in load_records(self.userdata) if r.model == "claude-sonnet-5-5")
        for name in ("Claude-Sonnet-5-5[1m]", "CLAUDE-SONNET-5-5"):
            self.assertAlmostEqual(price(rates, record.__class__(**dict(record.__dict__, model=name))), 0.007, places=9)

    def test_an_empty_rate_model_is_unpriced_not_priced_by_model(self):
        rates = load_rates(self.userdata)
        record = next(r for r in load_records(self.userdata) if r.model == "claude-sonnet-5-5")
        self.assertEqual(price(rates, record.__class__(**dict(record.__dict__, rate_model=""))), 0.0)

    def test_bare_family_names_are_never_priced(self):
        rates = load_rates(self.userdata)
        record = next(r for r in load_records(self.userdata) if r.model == "claude-sonnet-5-5")
        self.assertEqual(price(rates, record.__class__(**dict(record.__dict__, model="sonnet", rate_model=None))), 0.0)


class SummaryTests(Case):
    def test_window_totals(self):
        s = self.summary()
        # 10000 + 300 + 11000 + 20 (claude) + 350 (codex) + 2006 (grok) + 3500 (cursor); the synthetic row is out, and so is
        # the 1 Sep record: the oldest day with no history yet, its first record at 09:00, so it is cut off (see test_history)
        self.assertEqual(s.total_tokens, 27176)
        # 0.02495 + 0.0022 + 0.007 + 0.5 (reported) + 0.000055 + 0 + 0.0123 (reported)
        self.assertAlmostEqual(s.total_cost_usd, 0.546505, places=6)
        self.assertEqual((s.first_day, s.last_day, s.updated), (date(2026, 9, 2), TODAY, TODAY))

    def test_daily_buckets_use_dhaka_days(self):
        by_day = {d.day: d.tokens for d in self.summary().daily}
        self.assertEqual(len(by_day), 47)  # 2 Sep to 18 Oct
        self.assertEqual(by_day[date(2026, 10, 18)], 3870)  # 20 + 350 + 3500; 01:00 Dhaka is still 17 Oct in UTC
        self.assertEqual(by_day[date(2026, 10, 17)], 10300)
        self.assertEqual(by_day[date(2026, 10, 16)], 11000)
        self.assertEqual(by_day[date(2026, 10, 15)], 2006)
        self.assertEqual(by_day[date(2026, 9, 2)], 0)

    def test_split_by_tool(self):
        tools = [(t.label, t.tokens) for t in self.summary().tools]
        self.assertEqual(tools, [("Claude Code", 21320), ("Cursor", 3500), ("Grok", 2006), ("Codex", 350)])

    def test_top_six_models_and_the_rest_as_other(self):
        models = [(m.label, m.tokens) for m in self.summary().models]
        self.assertEqual(models, [("claude-sonnet-5-5", 11000), ("claude-opus-5-5", 10320),
                                  ("cursor-grok-4.6-high-fast", 3500), ("mystery-model", 2000),
                                  ("gpt-6-luna", 350), ("tiny-3", 3), ("other", 3)])
        opus = next(m for m in self.summary().models if m.label == "claude-opus-5-5")
        self.assertAlmostEqual(opus.cost_usd, 0.02495 + 0.0022 + 0.5, places=6)

    def test_unsafe_names_and_the_tail_share_one_other_row(self):
        scan = fx.scan_cache()
        scan["models"] = scan["models"][:]
        scan["models"][4] = "stealth/ox-alpha"  # mystery-model, 2000 tokens, now unsafe
        fx.write_userdata(self.userdata, scan=scan)
        models = [(m.label, m.tokens) for m in self.summary().models]
        self.assertEqual(models, [("claude-sonnet-5-5", 11000), ("claude-opus-5-5", 10320),
                                  ("cursor-grok-4.6-high-fast", 3500), ("gpt-6-luna", 350),
                                  ("tiny-3", 3), ("tiny-2", 2), ("other", 2001)])

    def test_odd_model_names_are_shown_as_other(self):
        scan = fx.scan_cache()
        scan["models"] = scan["models"][:]
        scan["models"][4] = "/Users/someone/secret"
        fx.write_userdata(self.userdata, scan=scan)
        labels = [m.label for m in self.summary().models]
        self.assertNotIn("/Users/someone/secret", labels)
        self.assertNotIn("mystery-model", labels)
        self.assertIn("other", labels)


class RenderTests(Case):
    def test_both_themes_are_valid_svg_with_the_required_text(self):
        for theme in (LIGHT, DARK):
            svg = render(self.summary(), theme)
            ET.fromstring(svg)
            for text in ("API-equivalent", "updated 18 Oct 2026", "27.2K", "$0.55", "claude-opus-5-5", "Claude Code"):
                self.assertIn(text, svg)
        self.assertNotEqual(render(self.summary(), LIGHT), render(self.summary(), DARK))

    def test_the_phone_card_is_valid_svg_400_wide_with_the_same_numbers(self):
        for theme in (LIGHT, DARK):
            svg = render_phone(self.summary(), theme)
            root = ET.fromstring(svg)
            self.assertEqual(root.get("width"), "400")
            self.assertEqual(root.get("viewBox").split()[:3], ["0", "0", "400"])
            for text in ("API-equivalent", "updated 18 Oct 2026", "27.2K", "$0.55", "claude-opus-5-5", "Claude Code", "TOP MODELS", "BY TOOL"):
                self.assertIn(text, svg)
        self.assertNotEqual(render_phone(self.summary(), LIGHT), render_phone(self.summary(), DARK))

    def test_phone_text_is_never_smaller_than_15_so_it_stays_about_11px_at_294_wide(self):
        for theme in (LIGHT, DARK):
            sizes = [int(size) for size in re.findall(r'font-size="(\d+)"', render_phone(self.summary(), theme))]
            self.assertGreaterEqual(min(sizes), 15)

    def test_the_phone_card_grows_with_its_rows_and_keeps_them_inside(self):
        full = ET.fromstring(render_phone(self.summary(), DARK))
        empty = ET.fromstring(render_phone(summarise([], load_rates(self.userdata), TODAY), DARK))
        height = lambda root: float(root.get("viewBox").split()[3])
        self.assertGreater(height(full), height(empty))
        for root in (full, empty):
            ys = [float(node.get("y")) for node in root.iter("{http://www.w3.org/2000/svg}text")]
            self.assertLess(max(ys) + 8, height(root))  # descenders too
            bars = [node for node in root.iter("{http://www.w3.org/2000/svg}rect") if node.get("height") == "3"]
            if bars:  # the model share bars, the lowest thing on the card
                bottom = max(float(node.get("y")) + 3 for node in bars)
                self.assertLessEqual(bottom + 12, height(root))

    def test_a_seven_figure_phone_cost_is_drawn_smaller_so_it_stays_inside(self):
        from dataclasses import replace
        sizes = {}
        for cost in (19123.4, 123456.0, 1234567.0):
            svg = render_phone(replace(self.summary(), total_cost_usd=cost), DARK)
            sizes[fmt_cost(cost)] = re.search(r'font-size="(\d+)"[^>]*>%s<' % re.escape(fmt_cost(cost)), svg).group(1)
        self.assertEqual(sizes, {"$19,123": "40", "$123,456": "40", "$1,234,567": "32"})

    def test_a_real_zero_is_drawn_as_zero(self):
        empty = summarise([], load_rates(self.userdata), TODAY)
        for draw in (render, render_phone):
            svg = draw(empty, DARK)
            ET.fromstring(svg)
            self.assertIn(">0<", svg)
            self.assertIn(">$0<", svg)
            self.assertIn("No usage in this period", svg)

    def test_number_formats(self):
        self.assertEqual([fmt_tokens(n) for n in (0, 999, 1234, 12345678, 123456789, 999960, 1_234_567_890)],
                         ["0", "999", "1.2K", "12.3M", "123M", "1.0M", "1.2B"])
        self.assertEqual([fmt_cost(c) for c in (0, 0.0123, 99.99, 99.996, 1234.5)], ["$0", "$0.01", "$99.99", "$100", "$1,234"])

    def test_privacy_guard_refuses_paths_and_ids(self):
        for bad in ("/Users/me/x", "msg_01ABC", "req_01ABC", "123e4567-e89b-12d3-a456-426614174000", "~/notes",
                    "/Volumes/Work/x", "/private/var/x", "/home/me/x", "3f2a9c41d07e4b8a9f1e6c2d5b7a0e93"):
            with self.assertRaises(PrivacyError, msg=bad):
                check_privacy("<svg>%s</svg>" % bad)
        check_privacy(render(self.summary(), DARK))
        check_privacy(render_phone(self.summary(), DARK))

    def test_a_model_named_like_an_id_is_shown_as_other(self):
        scan = fx.scan_cache()
        scan["models"] = scan["models"][:]
        scan["models"][4] = "123e4567-e89b-12d3-a456-426614174000"
        fx.write_userdata(self.userdata, scan=scan)
        for theme in (LIGHT, DARK):
            check_privacy(render(self.summary(), theme))
            check_privacy(render_phone(self.summary(), theme))
        self.assertNotIn("123e4567", render(self.summary(), DARK))
        self.assertNotIn("123e4567", render_phone(self.summary(), DARK))


class CliTests(Case):
    def run_cli(self, *extra, now=fx.NOW):
        args = ["--userdata", str(self.userdata), "--claude-projects", str(self.projects), "--log", str(self.tmp / "card.log"),
                "--history", str(self.tmp / "history.json")] + list(extra)
        return cli.main(args, now=now)

    FOUR = ["ai-usage-dark.svg", "ai-usage-light.svg", "ai-usage-phone-dark.svg", "ai-usage-phone-light.svg"]

    def test_writes_all_four_files_to_out(self):
        self.assertEqual(self.run_cli("--out", str(self.tmp / "out")), 0)
        self.assertEqual(sorted(p.name for p in (self.tmp / "out").iterdir()), self.FOUR)

    def test_refuses_when_the_newest_record_is_older_than_48_hours(self):
        out = self.tmp / "out"
        self.assertEqual(self.run_cli("--out", str(out), now=fx.NOW + timedelta(hours=49)), 1)
        self.assertFalse(out.exists())
        self.assertIn("no source has recent usage", (self.tmp / "card.log").read_text())

    def test_the_window_ends_today_even_when_the_newest_record_is_older(self):
        out = self.tmp / "out"
        # 40 hours later it is 19 Oct: Claude Code's transcripts are live, so 19 Oct is drawn (as zero), not dropped.
        self.assertEqual(self.run_cli("--out", str(out), now=fx.NOW + timedelta(hours=40)), 0)
        svg = (out / "ai-usage-dark.svg").read_text()
        self.assertIn("updated 18 Oct 2026", svg)
        self.assertIn(">19 Oct<", svg)

    def test_a_stale_t3_cache_alone_warns_but_does_not_refuse(self):
        # T3's scan cache ends 18 Oct 02:00 Dhaka, 54 hours before now; Claude Code has a reply an hour before now.
        now = fx.NOW + timedelta(hours=52)
        fx.write_lines(self.projects / "-proj-a" / "chat3.jsonl",
                       [fx.reply("claude-opus-5-5", (10, 20, 1), fx.usage(10, 10), msg="msg_n", req="req_n")], mtime_ms=fx.ms(10, 20, 4))
        out = self.tmp / "out"
        self.assertEqual(self.run_cli("--out", str(out), now=now), 0)
        self.assertEqual(len(list(out.iterdir())), 4)
        self.assertIn("WARNING T3 Code's caches are 54 hours old", (self.tmp / "card.log").read_text())

    def test_future_and_out_of_range_timestamps_are_ignored(self):
        scan = fx.scan_cache()
        scan["files"]["/f/z.jsonl"] = fx.entry("grok", [
            fx.row(10 ** 15, "claude-sonnet-5-5", 0, 1, 0, 0, 0, "msg_huge:req_huge"),
            fx.row(fx.ms(10, 25, 1), "claude-sonnet-5-5", 0, 1, 0, 0, 0, "msg_future:req_future")])
        fx.write_userdata(self.userdata, scan=scan)
        out = self.tmp / "out"
        self.assertEqual(self.run_cli("--out", str(out)), 0)
        self.assertIn("updated 18 Oct 2026", (out / "ai-usage-dark.svg").read_text())
        # with only a future record, the age check still refuses
        self.assertEqual(self.run_cli("--out", str(out), now=fx.NOW + timedelta(hours=49)), 1)

    def test_a_future_dated_transcript_line_does_not_hide_a_stale_card(self):
        fx.write_lines(self.projects / "-proj-a" / "chat4.jsonl",
                       [fx.reply("claude-opus-5-5", (1, 1, 1, 2030), fx.usage(10, 10), msg="msg_2030", req="req_2030")], mtime_ms=fx.ms(10, 18, 3))
        self.assertEqual(self.run_cli("--out", str(self.tmp / "out"), now=fx.NOW + timedelta(hours=49)), 1)
        self.assertIn("no source has recent usage", (self.tmp / "card.log").read_text())

    def test_a_missing_transcripts_folder_publishes_nothing(self):
        out = self.tmp / "out"
        self.assertEqual(self.run_cli("--out", str(out), "--claude-projects", str(self.tmp / "missing")), 1)
        self.assertFalse(out.exists())
        self.assertIn("no Claude Code transcripts folder found", (self.tmp / "card.log").read_text())

    def test_refuses_when_the_privacy_guard_trips_on_any_of_the_four(self):
        for name in ("render", "render_phone"):
            out = self.tmp / ("out-" + name)
            with mock.patch.object(cli, name, return_value="<svg>/Users/someone</svg>"):
                self.assertEqual(self.run_cli("--out", str(out)), 1)
            self.assertFalse(out.exists())
        self.assertIn("PrivacyError", (self.tmp / "card.log").read_text())

    def test_a_blocked_transcripts_folder_refuses_and_names_the_macos_privacy_setting(self):
        denied = PermissionError(1, "Operation not permitted")
        with mock.patch("ai_usage_card.claude.os.walk", side_effect=lambda root, onerror, **kw: onerror(denied)):
            self.assertEqual(self.run_cli("--out", str(self.tmp / "out")), 1)
        log = (self.tmp / "card.log").read_text()
        self.assertIn("cannot be read (PermissionError): macOS may be blocking this job from the drive", log)
        self.assertFalse((self.tmp / "out").exists())

    def test_refuses_an_unsupported_cache_version(self):
        scan = fx.scan_cache()
        scan["version"] = 10
        fx.write_userdata(self.userdata, scan=scan)
        self.assertEqual(self.run_cli("--out", str(self.tmp / "out")), 1)
        self.assertIn("unsupported scan cache version", (self.tmp / "card.log").read_text())

    def test_publishes_once_to_an_orphan_branch_and_is_idempotent(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(remote)], check=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        publish_args = ["--repo", str(remote), "--clone", str(self.tmp / "clone"), "--git-credential-helper", ""]

        def git(*args):
            return subprocess.run(["/usr/bin/git", "--git-dir", str(remote)] + list(args), check=True, stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout.strip()

        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_cli(*publish_args), 0)
            self.assertEqual(self.run_cli(*publish_args), 0)  # no new data: no new commit
            self.assertEqual(git("rev-list", "--count", "usage-card"), "1")
            self.assertEqual(git("ls-tree", "--name-only", "usage-card").split("\n"), self.FOUR)
            self.assertEqual(git("log", "-1", "--format=%s", "usage-card"), "Update AI usage card")
            self.assertEqual(git("rev-list", "--max-parents=0", "usage-card"), git("rev-parse", "usage-card"))  # the commit is the orphan root
            # a new record changes the card: exactly one more commit
            scan = fx.scan_cache()
            scan["files"]["/f/f.jsonl"] = fx.entry("grok", [fx.row(fx.ms(10, 18, 3), "claude-sonnet-5-5", 0, 1, 0, 0, 1, "msg_z:req_z")])
            fx.write_userdata(self.userdata, scan=scan)
            self.assertEqual(self.run_cli(*publish_args), 0)
            self.assertEqual(git("rev-list", "--count", "usage-card"), "2")

    def test_a_commit_whose_push_failed_is_pushed_on_the_next_run(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(remote)], check=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        publish_args = ["--repo", str(remote), "--clone", str(self.tmp / "clone"), "--git-credential-helper", ""]
        real_git = publish._git

        def push_fails(clone, *args, **kwargs):
            if args[0] == "push":
                raise publish.PublishError("git push failed: network")
            return real_git(clone, *args, **kwargs)

        def remote_branch():
            return subprocess.run(["/usr/bin/git", "--git-dir", str(remote), "branch", "--list", "usage-card"],
                                  stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()

        with mock.patch.dict(os.environ, env):
            with mock.patch.object(publish, "_git", side_effect=push_fails):
                self.assertEqual(self.run_cli(*publish_args), 1)
            self.assertEqual(remote_branch(), "")
            self.assertEqual(self.run_cli(*publish_args), 0)  # same bytes, but the commit was never pushed
            self.assertEqual(remote_branch(), "usage-card")
            # and when the remote branch is deleted, the next run puts it back
            subprocess.run(["/usr/bin/git", "--git-dir", str(remote), "branch", "-q", "-D", "usage-card"], check=True)
            self.assertEqual(self.run_cli(*publish_args), 0)
            self.assertEqual(remote_branch(), "usage-card")

    def _publish_setup(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(remote)], check=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        publish_args = ["--repo", str(remote), "--clone", str(self.tmp / "clone"), "--git-credential-helper", ""]

        def other_clone_pushes(orphan):
            """Another clone pushes to the branch: a normal commit on top, or (orphan) a rebuilt history."""
            other = self.tmp / ("other-%d" % len(list(self.tmp.glob("other-*"))))
            subprocess.run(["/usr/bin/git", "clone", "-q", "-b", "usage-card", str(remote), str(other)], check=True)
            run = lambda *a: subprocess.run(["/usr/bin/git"] + list(a), cwd=str(other), check=True, stdout=subprocess.PIPE)
            if orphan:
                run("checkout", "-q", "--orphan", "rebuilt")
            (other / "ai-usage-dark.svg").write_text("<svg>by hand</svg>\n", encoding="utf-8")
            run("add", "--", "ai-usage-dark.svg")
            run("commit", "-q", "-m", "by hand")
            run("push", "-q", *(["--force"] if orphan else []), "origin", "HEAD:refs/heads/usage-card")

        def tip_file(name):
            return subprocess.run(["/usr/bin/git", "--git-dir", str(remote), "show", "usage-card:" + name], check=True,
                                  stdout=subprocess.PIPE, universal_newlines=True).stdout
        return env, publish_args, other_clone_pushes, tip_file

    def test_a_rebuilt_remote_branch_is_taken_as_the_base_and_the_card_republished(self):
        env, publish_args, other_clone_pushes, tip_file = self._publish_setup()
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_cli(*publish_args), 0)
            other_clone_pushes(orphan=True)  # the branch no longer shares history with the working clone
            self.assertEqual(self.run_cli(*publish_args), 0)
            cards = cli.build_cards(self.userdata, fx.NOW, self.projects, history.load(self.tmp / "history.json"))
            for name in self.FOUR:
                self.assertEqual(tip_file(name), cards[name])

    def test_a_write_that_failed_part_way_does_not_block_later_runs(self):
        env, publish_args, other_clone_pushes, tip_file = self._publish_setup()
        real_write = Path.write_text
        calls = []

        def third_write_fails(path, *args, **kwargs):
            calls.append(path.name)
            if len(calls) == 3:
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_cli(*publish_args), 0)
            scan = fx.scan_cache()
            scan["files"]["/f/f.jsonl"] = fx.entry("grok", [fx.row(fx.ms(10, 18, 3), "claude-sonnet-5-5", 0, 1, 0, 0, 1, "msg_z:req_z")])
            fx.write_userdata(self.userdata, scan=scan)
            with mock.patch.object(Path, "write_text", third_write_fails):
                self.assertEqual(self.run_cli(*publish_args), 1)  # two of the four files written, nothing committed
            other_clone_pushes(orphan=False)  # meanwhile the remote moves on and touches a half-written file
            self.assertEqual(self.run_cli(*publish_args), 0)
            cards = cli.build_cards(self.userdata, fx.NOW, self.projects, history.load(self.tmp / "history.json"))  # the new data, all four files
            for name in self.FOUR:
                self.assertEqual(tip_file(name), cards[name])

    def test_the_scheduled_run_saves_the_history_and_a_failed_save_still_publishes(self):
        env, publish_args, _, tip_file = self._publish_setup()
        args = ["--userdata", str(self.userdata), "--claude-projects", str(self.projects), "--log", str(self.tmp / "card.log")] + publish_args
        default = self.tmp / "default-history.json"
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(history.os, "replace", side_effect=OSError("read-only")):
                self.assertEqual(cli.main(args, now=fx.NOW), 1)  # the launchd run: no --out, no --history
            self.assertFalse(default.exists())
            self.assertIn("<svg", tip_file("ai-usage-dark.svg"))  # the card went out before the save failed
            self.assertIn("refused: LedgerError: history file %s cannot be written (OSError: read-only)" % default,
                          (self.tmp / "card.log").read_text())
            self.assertEqual(cli.main(args, now=fx.NOW), 0)
        self.assertEqual(history.load(default).start, date(2026, 8, 20))  # 18 Oct - 59 days, the cleanup point

    def test_a_refusal_publishes_nothing(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(remote)], check=True)
        code = self.run_cli("--repo", str(remote), "--clone", str(self.tmp / "clone"), "--git-credential-helper", "",
                            now=fx.NOW + timedelta(days=5))
        self.assertEqual(code, 1)
        branches = subprocess.run(["/usr/bin/git", "--git-dir", str(remote), "branch", "--list"], stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout
        self.assertEqual(branches.strip(), "")
        self.assertFalse((self.tmp / "clone").exists())


if __name__ == "__main__":
    unittest.main()
