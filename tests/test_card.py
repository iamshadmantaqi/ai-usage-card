from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ai_usage_card import cli, history, publish
from ai_usage_card.claude import _timestamp_ms, load_claude_records, transcript_files
from ai_usage_card.load import CacheError, claude_reply_key, load_records, load_t3
from ai_usage_card.pricing import load_rates, parse_rate_table, price
from ai_usage_card.privacy import PrivacyError, check_privacy
from ai_usage_card.refresh import Rescan
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

    def test_cursor_usage_counts_without_any_settings_file(self):
        self.assertFalse((self.userdata / "settings.json").exists())
        self.assertEqual([r.tokens.total for r in load_records(self.userdata) if r.provider == "cursor"], [3500])

    def test_every_cursor_key_counts_and_a_repeated_dedupe_key_counts_once(self):
        def entry(account, fetched, rows):
            return {"accountKey": account, "sinceMs": 0, "untilMs": fx.MTIME, "fetchedAtMs": fetched, "records": rows}
        t = fx.ms(10, 18, 3)
        cursor = {"version": 2, "strings": ["cursor-grok-4.6-high-fast", "s"], "accounts": {
            "api-key:aaa": entry("one", 5, [[t, 0, 1, 1000, 0, 0, 0, None, "cursor-account:one:1"],
                                           [t, 0, 1, 20, 0, 0, 0, None, "cursor-account:shared:1"]]),
            "api-key:bbb": entry("two", 6, [[t, 0, 1, 300, 0, 0, 0, None, "cursor-account:two:1"],
                                           [t, 0, 1, 20, 0, 0, 0, None, "cursor-account:shared:1"]]),
            "api-key:ccc": entry("two", 2, [[t, 0, 1, 9999, 0, 0, 0, None, "cursor-account:stale:1"]])}}
        fx.write_userdata(self.userdata, cursor=cursor)
        # one: 1000 + 20; two: 300 (its second copy of "shared" is dropped); the older entry of "two" is ignored
        self.assertEqual(sorted(r.tokens.total for r in load_records(self.userdata) if r.provider == "cursor"), [20, 300, 1000])

    def test_the_earliest_cursor_cache_end_holds_back_freezing(self):
        cursor = fx.cursor_cache()
        cursor["accounts"]["other"] = dict(cursor["accounts"]["keychain"], accountKey="o", untilMs=fx.ms(9, 1, 0), records=[])
        fx.write_userdata(self.userdata, cursor=cursor)
        self.assertEqual(load_t3(self.userdata)[2], fx.ms(9, 1, 0))

    def test_a_version_1_cursor_cache_is_still_read_when_there_is_no_version_2(self):
        fx.write_userdata(self.tmp / "userdata1", cursor_version=1)
        records = load_records(self.tmp / "userdata1")
        self.assertEqual([r.tokens.total for r in records if r.provider == "cursor"], [3500])

    def test_the_version_2_cursor_cache_wins_over_a_version_1_file_left_behind(self):
        old = fx.cursor_cache()
        old["accounts"]["keychain"]["records"] = [[fx.ms(10, 17, 3), 0, 1, 7, 0, 0, 0, None, "cursor-account:k:old"]]
        (self.userdata / "usage-cursor-account-cache-v1.json").write_text(json.dumps(dict(old, version=1)))
        self.assertEqual([r.tokens.total for r in load_records(self.userdata) if r.provider == "cursor"], [3500])

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

    def test_a_newer_cursor_cache_version_is_refused(self):
        (self.userdata / "usage-cursor-account-cache-v3.json").write_text("{}")
        with self.assertRaises(CacheError) as caught:
            load_records(self.userdata)
        self.assertIn("T3 Code now writes usage-cursor-account-cache-v3.json", str(caught.exception))
        (self.userdata / "usage-cursor-account-cache-v3.json").unlink()
        (self.userdata / "usage-scan-cache-v4.json").write_text("{}")  # an older scan cache left behind is fine
        load_records(self.userdata)

    def test_reads_a_version_17_cache_with_literal_totals(self):
        u17 = fx.write_userdata(self.tmp / "u17", scan=fx.scan_cache_v17(), scan_version=17)
        records = load_records(u17)  # a checked version logs no warning (the unknown-version test asserts the opposite)
        by_key = {r.dedupe_key: r for r in records if r.dedupe_key}
        self.assertEqual(by_key["msg_a:req_a"].tokens.total, 10000)
        self.assertEqual(by_key["msg_a:req_a"].tokens.cache_creation_1h, 600)  # the fourteenth row field
        self.assertEqual(by_key["msg_b:req_b"].tokens.total, 300)
        self.assertEqual(by_key["msg_b:req_b"].speed, "fast")
        self.assertEqual(by_key["msg_d:req_d"].reported_cost_usd, 0.5)
        self.assertEqual(sorted(r.tokens.total for r in records if r.provider == "codex"), [350])
        self.assertEqual(sorted(r.tokens.total for r in records if r.provider == "grok"), [1, 2, 3, 2000])
        v5 = sorted((r.provider, r.model, r.tokens.total) for r in load_records(self.userdata) if r.provider != "cursor")
        self.assertEqual(sorted((r.provider, r.model, r.tokens.total) for r in records if r.provider != "cursor"), v5)

    def test_an_unknown_newer_version_with_a_matching_structure_is_read_with_a_warning(self):
        u18 = fx.write_userdata(self.tmp / "u18", scan=fx.scan_cache_v17(version=18), scan_version=18)
        with self.assertLogs("ai-usage-card", level="WARNING") as logged:
            records = load_records(u18)
        self.assertIn("usage-scan-cache-v18.json", logged.output[0])
        self.assertIn("newer than the version 17", logged.output[0])
        self.assertEqual(sorted(r.tokens.total for r in records if r.provider == "codex"), [350])
        self.assertEqual({r.dedupe_key: r.tokens.total for r in records}["msg_a:req_a"], 10000)

    def test_an_unknown_newer_version_with_a_broken_structure_is_refused(self):
        bad_row = fx.scan_cache_v17(version=18)
        bad_row["files"]["/f/a.jsonl"]["r"][0][3] = "lots"  # a usage field that is not a number
        short_row = fx.scan_cache_v17(version=18)
        short_row["files"]["/f/a.jsonl"]["r"][0] = short_row["files"]["/f/a.jsonl"]["r"][0][:8]
        bad_entry = fx.scan_cache_v17(version=18)
        del bad_entry["files"]["/f/c.jsonl"]["o"]
        no_models = fx.scan_cache_v17(version=18)
        no_models["models"] = {"0": "x"}
        wide_row = fx.scan_cache_v17(version=18)
        wide_row["files"]["/f/b.jsonl"]["r"][1].append(7)  # a fifteenth field nobody has checked
        cases = (("bad_row", bad_row, "version 18 has rows"), ("short_row", short_row, "version 18 has rows"),
                 ("wide_row", wide_row, "version 18 has rows"), ("bad_entry", bad_entry, "version 18 has a file entry"),
                 ("no_models", no_models, "unexpected shape"))
        for name, scan, message in cases:
            folder = fx.write_userdata(self.tmp / name, scan=scan, scan_version=18)
            with self.assertRaisesRegex(CacheError, message, msg=name):
                load_records(folder)

    def test_rows_t3_would_reject_drop_their_file_in_a_checked_version(self):
        for name, field, value in (("null_one_hour", 13, None), ("number_turn_id", 12, 5)):
            scan = fx.scan_cache_v17()
            scan["files"]["/f/b.jsonl"]["r"][1][field] = value
            folder = fx.write_userdata(self.tmp / name, scan=scan, scan_version=17)
            keys = {r.dedupe_key: r.tokens.total for r in load_records(folder)}
            self.assertNotIn("msg_d:req_d", keys, msg=name)  # b.jsonl dropped
            self.assertEqual(keys["msg_a:req_a"], 10000, msg=name)

    def test_a_wider_row_in_a_checked_version_is_read_as_t3_reads_it(self):
        scan = fx.scan_cache_v17()
        scan["files"]["/f/b.jsonl"]["r"][1].append(7)
        folder = fx.write_userdata(self.tmp / "u17wide", scan=scan, scan_version=17)
        self.assertEqual({r.dedupe_key: r.tokens.total for r in load_records(folder)}["msg_d:req_d"], 20)

    def test_the_cache_saved_last_is_read_when_an_older_t3_left_two(self):
        folder = fx.write_userdata(self.tmp / "two", scan=fx.scan_cache_v17(), scan_version=17)
        older = fx.scan_cache_v9()
        older["version"] = 16
        older["files"] = {"/f/c.jsonl": older["files"]["/f/c.jsonl"]}
        (folder / "usage-scan-cache-v16.json").write_text(json.dumps(older))
        os.utime(folder / "usage-scan-cache-v17.json", (1_000_000, 1_000_000))  # frozen long ago
        self.assertEqual([r.provider for r in load_records(folder) if r.provider != "cursor"], ["codex"])  # v16 only
        os.utime(folder / "usage-scan-cache-v16.json", (500_000, 500_000))  # a leftover from before the upgrade
        self.assertEqual(len([r for r in load_records(folder) if r.provider == "grok"]), 4)  # v17 again

    def test_grok_keys_from_before_version_17_get_the_time_added(self):
        def grok_keys(version):
            scan = fx.scan_cache_v17(version=version)
            e1 = list(fx.E1)
            e1[8] = "grok-turn:mystery-model"  # T3 16 wrote Grok keys without the time
            twin = list(e1)
            twin[0] += 1000  # a second reply with the same old key
            scan["files"]["/f/e.jsonl"]["r"] = [e1, twin]
            folder = fx.write_userdata(self.tmp / ("grok%d" % version), scan=scan, scan_version=version)
            return sorted(r.dedupe_key for r in load_records(folder) if r.provider == "grok")
        e1_time = fx.E1[0]
        self.assertEqual(grok_keys(16), ["grok-turn:%d:mystery-model" % e1_time, "grok-turn:%d:mystery-model" % (e1_time + 1000)])
        self.assertEqual(grok_keys(17), ["grok-turn:mystery-model"])  # version 17 keys are taken as written, so the twin is a copy

    def test_a_checked_version_with_one_bad_file_still_only_drops_that_file(self):
        scan = fx.scan_cache_v17()
        scan["files"]["/f/b.jsonl"]["r"][1][3] = "lots"
        folder = fx.write_userdata(self.tmp / "u17bad", scan=scan, scan_version=17)
        keys = {r.dedupe_key: r.tokens.total for r in load_records(folder)}
        self.assertNotIn("msg_d:req_d", keys)  # only b.jsonl has it
        self.assertEqual(keys["msg_a:req_a"], 10000)  # a.jsonl still has it

    def test_a_file_whose_version_field_is_not_the_one_in_its_name_is_refused(self):
        scan = fx.scan_cache_v9()
        scan["version"] = 18
        fx.write_userdata(self.tmp / "u18", scan=scan, scan_version=9)
        with self.assertRaises(CacheError):
            load_records(self.tmp / "u18")

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
        self.assertIn("cache file is missing: usage-scan-cache-v17.json", str(caught.exception))
        self.assertNotIn("now writes", str(caught.exception))

    def test_files_with_a_read_position_t3_rejects_are_skipped(self):
        scan = fx.scan_cache()
        scan["files"]["/f/e.jsonl"]["gl"] = 2000  # longer than the offset `o`
        fx.write_userdata(self.userdata, scan=scan)
        self.assertEqual([r for r in load_records(self.userdata) if r.provider == "grok"], [])

    def test_missing_cursor_file_means_no_cursor_records(self):
        (self.userdata / "usage-cursor-account-cache-v2.json").unlink()
        self.assertEqual([r for r in load_records(self.userdata) if r.provider == "cursor"], [])

    def test_cursor_default_model_is_shown_as_cursor_auto_at_the_same_cost(self):
        strings = ["default", "agent-session"]
        rows = [[fx.ms(10, 18, 3), 0, 1, 1000, 2000, 0, 500, None, "cursor-account:k:1:auto"]]
        cursor = {"version": 2, "strings": strings, "accounts": {"keychain": {
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
        # 2 is the 1 Aug transcript, inside the six-month floor now (summarise starts at the cleanup point, see test_history)
        self.assertEqual(sorted(r.tokens.total for r in records if r.provider == "claude"), [2, 100, 170, 300, 5100])

    def test_refuses_when_t3_has_claude_usage_but_the_transcripts_give_none(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(cli.Refused, "transcripts gave none"):
            cli.gather(self.userdata, empty, fx.NOW)

    def test_a_stale_cursor_cache_holds_back_the_freeze_even_with_a_fresh_scan_cache(self):
        cursor = fx.cursor_cache()
        # T3 last read Cursor on 25 Sep, 23 days before today: its usage since then may still arrive
        cursor["accounts"]["keychain"].update(untilMs=fx.ms(9, 25, 0), fetchedAtMs=fx.ms(9, 25, 0))
        fx.write_userdata(self.userdata, cursor=cursor)
        self.assertEqual(cli.gather(self.userdata, self.projects, fx.NOW)[1], fx.ms(9, 25, 0))
        (self.userdata / "usage-cursor-account-cache-v2.json").unlink()  # no Cursor login: the scan cache alone decides
        self.assertEqual(cli.gather(self.userdata, self.projects, fx.NOW)[1], fx.ms(10, 18, 2))

    def test_a_cursor_account_not_read_for_30_days_no_longer_holds_back_the_freeze(self):
        cursor = fx.cursor_cache()
        # a key T3 stopped reading on 1 Sep (47 days before today) stays in the file; its records still count
        cursor["accounts"]["gone"] = dict(cursor["accounts"]["keychain"], accountKey="gone", untilMs=fx.ms(9, 1, 0),
                                          fetchedAtMs=fx.ms(9, 1, 0),
                                          records=[[fx.ms(8, 31, 3), 0, 1, 70, 0, 0, 0, None, "cursor-account:gone:1:0"]])
        fx.write_userdata(self.userdata, cursor=cursor)
        records, through = cli.gather(self.userdata, self.projects, fx.NOW)
        self.assertEqual(sorted(r.tokens.total for r in records if r.provider == "cursor"), [70, 3500])
        self.assertEqual(through, fx.ms(10, 18, 2))  # the live account and the scan cache decide
        del cursor["accounts"]["keychain"]  # the only account is the stale one: the scan cache alone decides
        fx.write_userdata(self.userdata, cursor=cursor)
        self.assertEqual(cli.gather(self.userdata, self.projects, fx.NOW)[1], fx.ms(10, 18, 2))

    def test_the_forced_freeze_warning_names_the_newest_day_it_freezes(self):
        cursor = fx.cursor_cache()
        cursor["accounts"]["keychain"]["untilMs"] = fx.ms(7, 1, 12)  # caches end 30 Jun; 18 Oct - 50 days is 29 Aug
        fx.write_userdata(self.userdata, cursor=cursor)
        with self.assertLogs("ai-usage-card", "WARNING") as logs:
            cli.build(self.userdata, fx.NOW, self.projects)
        self.assertIn("T3 Code's caches end 2026-06-30: days up to 2026-08-29 are frozen without waiting", "\n".join(logs.output))

    def test_t3s_claude_records_are_ignored_but_its_other_tools_still_count(self):
        records, t3_newest = cli.gather(self.userdata, self.projects, fx.NOW)
        s = summarise(records, load_rates(self.userdata), TODAY)
        self.assertEqual([(t.label, t.tokens) for t in s.tools],
                         [("Claude Code", 5570), ("Cursor", 3500), ("Grok", 2006), ("Codex", 350)])
        self.assertEqual(s.total_tokens, 5570 + 3500 + 2006 + 350)
        # 0.0169 + 0.00055 + 0.0022 (claude) + 0.0123 (cursor, reported) + 0.000055 (codex) + 0 (grok, unpriced)
        self.assertAlmostEqual(s.total_cost_usd, 0.032005, places=6)
        self.assertEqual(t3_newest, fx.ms(10, 18, 2))  # the Codex row: the scan cache ends before Cursor's untilMs (03:00)


class ClaudeFromT3Tests(Case):
    """Claude Code from T3's scan cache, and the fallback to the transcripts. The T3 rows below are the fixture
    transcripts' replies as T3 17 stores them (every streamed copy, key `message.id:requestId`), so both paths
    must give the same numbers: X 5100 tokens $0.0169, Y 170 $0.00055, F 300 $0.0022, and the 1 Aug reply 2 tokens."""

    LIVE_SINCE = date(2026, 9, 18)  # 30 days before 18 Oct: the oldest day a full history leaves live
    CLEAN = Rescan(saved=True, claude_trouble=None)

    @staticmethod
    def utc(month, day, hour):
        return int(datetime(2026, month, day, hour, 0, 0, 123000, tzinfo=timezone.utc).timestamp() * 1000)

    def t3_scan(self, sources=None, covered=None):
        models = ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-6-luna"]

        def row(ts, model, uncached, cached, creation, output, key, one_hour=0, speed=fx.STANDARD, cost=None):
            return [ts, models.index(model), 0, uncached, cached, creation, output, 0, key, cost, speed, None, None, one_hour]
        x = lambda output: row(self.utc(10, 17, 6), "claude-opus-5-5", 100, 1000, 1000, output, "msg_x:req_x", one_hour=600)
        folder = str(self.projects)
        return {
            "version": 17, "models": models, "sessions": ["s"],
            "sources": sources if sources is not None else {"claude\0/demo/.claude-alt/projects": {"dir": folder}},
            "coveredSince": covered if covered is not None else {"claude\0" + folder: fx.ms(8, 1, 0)},
            "files": {
                "/f/chat1.jsonl": fx.entry("claude", [x(10), x(200),  # two streamed copies, the last one not final
                                                      row(self.utc(10, 17, 7), "claude-opus-5-5", 100, 0, 0, 200, "msg_f:req_f", speed=fx.FAST, cost=9.0)]),
                "/f/chat2.jsonl": fx.entry("claude", [x(3000)]),  # the resumed chat's final copy
                "/f/agent-1.jsonl": fx.entry("claude", [row(self.utc(10, 16, 5), "claude-sonnet-5-5", 50, 0, 100, 20, "msg_y:req_y")]),
                "/f/old.jsonl": fx.entry("claude", [row(self.utc(8, 1, 5), "claude-opus-5-5", 1, 0, 0, 1, "msg_o:req_o")]),
                "/f/codex.jsonl": fx.entry("codex", [row(fx.ms(10, 18, 2), "gpt-6-luna", 100, 200, 0, 50, None)]),
            },
        }

    def use(self, scan):
        for old in self.userdata.glob("usage-scan-cache-v*.json"):
            old.unlink()
        fx.write_userdata(self.userdata, scan=scan, scan_version=17)

    def gather(self, live_since, rescan=CLEAN):
        with self.assertLogs("ai-usage-card", "INFO") as logs:
            records, _ = cli.gather(self.userdata, self.projects, fx.NOW, rescan, live_since)
        return sorted(r.tokens.total for r in records if r.provider == "claude"), "\n".join(logs.output), records

    def test_t3_rows_give_the_same_tokens_and_cost_as_the_transcripts(self):
        self.use(self.t3_scan())
        with mock.patch.object(cli, "load_claude_records", side_effect=AssertionError("transcripts read")):
            totals, log, records = self.gather(self.LIVE_SINCE)
        self.assertIn("Claude Code read from T3 Code's scan cache", log)
        self.assertEqual(totals, [2, 170, 300, 5100])
        x = next(r for r in records if r.tokens.total == 5100)
        self.assertEqual((x.tokens.output, x.tokens.cache_creation_1h, x.timestamp_ms), (3000, 600, self.utc(10, 17, 6)))
        self.assertEqual({r.reported_cost_usd for r in records if r.provider == "claude"}, {None})  # F's reported $9 is dropped
        from_t3 = summarise(records, load_rates(self.userdata), TODAY)
        transcripts, _, _ = self.gather(self.LIVE_SINCE, None)
        self.assertEqual(transcripts, [2, 170, 300, 5100])
        from_transcripts = summarise(cli.gather(self.userdata, self.projects, fx.NOW)[0], load_rates(self.userdata), TODAY)
        for s in (from_t3, from_transcripts):
            self.assertEqual([(t.label, t.tokens) for t in s.tools], [("Claude Code", 5570), ("Cursor", 3500), ("Codex", 350)])
            # Claude Code 0.0169 + 0.00055 + 0.0022, Cursor 0.0123 (reported), Codex 0.000055
            self.assertAlmostEqual(s.total_cost_usd, 0.032005, places=9)

    def test_a_scan_that_starts_on_the_first_live_day_is_enough(self):
        start = int(datetime(2026, 9, 18, tzinfo=cli.TIMEZONE).timestamp() * 1000)
        self.use(self.t3_scan(covered={"claude\0" + str(self.projects): start}))
        self.assertIn("scan cache", self.gather(self.LIVE_SINCE)[1])

    def test_falls_back_to_the_transcripts_with_the_reason(self):
        folder, elsewhere = str(self.projects), str(self.tmp / "elsewhere")
        cases = [
            (self.LIVE_SINCE, self.t3_scan(), "T3 Code did not rescan", None),
            (self.LIVE_SINCE, self.t3_scan(), "T3 Code's cache did not change after its rescan, so it may be older than the rescan",
             Rescan(saved=False, claude_trouble=None)),
            (self.LIVE_SINCE, self.t3_scan(), "T3 Code did not read a Claude Code folder in full (status partial)",
             Rescan(saved=True, claude_trouble="T3 Code did not read a Claude Code folder in full (status partial)")),
            (None, self.t3_scan(), "T3 Code's scan of a Claude Code folder starts 2026-08-01, and the card needs it from 2026-04-20",
             self.CLEAN),  # no history yet: the whole window is live
            (self.LIVE_SINCE, self.t3_scan(covered={"claude\0" + folder: fx.ms(9, 20, 0)}),
             "T3 Code's scan of a Claude Code folder starts 2026-09-20, and the card needs it from 2026-09-18"),
            (self.LIVE_SINCE, self.t3_scan(covered={}), "T3 Code's scan of a Claude Code folder has no start, and the card needs it from 2026-09-18"),
            (self.LIVE_SINCE, self.t3_scan(sources={"claude\0" + elsewhere: {"dir": elsewhere}},
                                           covered={"claude\0" + elsewhere: fx.ms(8, 1, 0)}),
             "T3 Code does not scan the Claude Code folder given to the card"),
            (self.LIVE_SINCE, self.t3_scan(sources={}), "T3 Code scans no Claude Code folder"),
        ]
        for case in cases:
            live_since, scan, reason, rescan = case if len(case) == 4 else case + (self.CLEAN,)
            with self.subTest(reason):
                self.use(scan)
                totals, log, _ = self.gather(live_since, rescan)
                self.assertIn("Claude Code read from its transcripts: " + reason, log)
                self.assertEqual(totals, [2, 170, 300, 5100])

    def test_a_second_claude_folder_must_reach_back_too(self):
        folder, other = str(self.projects), str(self.tmp / "other")
        self.use(self.t3_scan(sources={"claude\0" + folder: {"dir": folder}, "claude\0" + other: {"dir": other}},
                              covered={"claude\0" + folder: fx.ms(8, 1, 0), "claude\0" + other: fx.ms(10, 1, 0)}))
        self.assertIn("a Claude Code folder starts 2026-10-01", self.gather(self.LIVE_SINCE)[1])

    def test_streamed_copies_keep_each_figures_largest_and_the_first_time(self):
        scan = self.t3_scan()
        first = scan["files"]["/f/chat1.jsonl"]["r"][0]
        scan["files"]["/f/chat2.jsonl"]["r"] = [[first[0] + 1000] + first[1:4] + [900, 1000, 3000] + first[7:]]
        self.use(scan)
        x = next(r for r in load_records(self.userdata) if r.dedupe_key == "msg_x:req_x")
        self.assertEqual((x.tokens.uncached, x.tokens.cached, x.tokens.cache_creation, x.tokens.output, x.timestamp_ms),
                         (100, 1000, 1000, 3000, self.utc(10, 17, 6)))

    def test_build_passes_the_rescan_and_the_first_live_day(self):
        self.use(self.t3_scan())
        with mock.patch.object(cli, "gather", wraps=cli.gather) as gathered:
            cli.build(self.userdata, fx.NOW, self.projects, None, self.CLEAN)
            cli.build(self.userdata, fx.NOW, self.projects)
        # no history yet: every day of the window is live, back to 20 Apr, which T3's scan (from 1 Aug) does not reach
        self.assertEqual([c[0][3:] for c in gathered.call_args_list],
                         [(self.CLEAN, date(2026, 4, 20)), (None, date(2026, 4, 20))])

    def test_a_reply_is_counted_once_per_message_id_on_both_paths(self):
        # The 16 November case: a second copy of a reply written without its request id. T3 keys it
        # `msg_x:` beside `msg_x:req_x` and counts it twice; Anthropic bills the message once.
        scan = self.t3_scan()
        copy = list(scan["files"]["/f/chat2.jsonl"]["r"][0])
        copy[8] = "msg_x:"
        scan["files"]["/f/chat2.jsonl"]["r"].append(copy)
        self.use(scan)
        totals, _, _ = self.gather(self.LIVE_SINCE)
        self.assertEqual(totals, [2, 170, 300, 5100])
        line = {"type": "assistant", "timestamp": "2026-10-17T06:00:00.123Z",
                "message": {"id": "msg_x", "model": "claude-opus-5-5", "usage": {
                    "input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 3000,
                    "cache_creation": {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600}}}}
        with open(next(self.projects.rglob("*.jsonl")), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        transcripts, _, _ = self.gather(self.LIVE_SINCE, None)
        self.assertEqual(transcripts, [2, 170, 300, 5100])

    def test_claude_keys_keep_the_message_id_alone(self):
        self.assertEqual([claude_reply_key(k) for k in ("msg_a:req_a", "msg_a:", ":req_b", "msg_a:req_a:advisor:2", "msg_a::advisor:0")],
                         ["claude:m:msg_a", "claude:m:msg_a", "claude:r:req_b", "claude:m:msg_a:advisor:2", "claude:m:msg_a:advisor:0"])

    def test_transcript_copies_keep_each_figures_largest_and_advisor_parts_count(self):
        def line(ts, output, cache_read, iterations=None):
            usage = {"input_tokens": 10, "cache_read_input_tokens": cache_read, "output_tokens": output}
            if iterations is not None:
                usage["iterations"] = iterations
            return json.dumps({"type": "assistant", "timestamp": ts, "requestId": "req_q",
                               "message": {"id": "msg_q", "model": "claude-opus-5-5", "usage": usage}})
        advisor = {"type": "advisor_message", "model": "claude-sonnet-5-5", "input_tokens": 7, "output_tokens": 3}
        folder = self.tmp / "q"
        folder.mkdir()
        (folder / "q.jsonl").write_text("\n".join([
            line("2026-10-17T05:00:00Z", 5, 900),
            line("2026-10-17T05:00:09Z", 40, 0, [{"type": "message"}, advisor]),  # a later copy with less cache read
        ]) + "\n")
        five = int(datetime(2026, 10, 17, 5, tzinfo=timezone.utc).timestamp() * 1000)
        records = load_claude_records([folder])
        # the reply keeps its first copy's time, its largest cache read (900) and its largest output (40)
        self.assertEqual(sorted((r.model, r.tokens.uncached, r.tokens.cached, r.tokens.output, r.timestamp_ms) for r in records),
                         [("claude-opus-5-5", 10, 900, 40, five), ("claude-sonnet-5-5", 7, 0, 3, five + 9000)])

    def test_the_first_live_day_follows_the_history(self):
        from ai_usage_card.history import Ledger, Row
        from ai_usage_card.summarise import first_live_day
        rows = [Row("claude", "claude-opus-5-5", 1, 0.0)]
        days = {date(2026, 8, 30) + timedelta(days=n): rows for n in range(20)}  # 30 Aug to 18 Sep
        self.assertEqual(first_live_day(Ledger(days, date(2026, 8, 30)), TODAY), date(2026, 9, 19))
        del days[date(2026, 9, 4)]  # a day with no usage is never saved, so it stays live
        self.assertEqual(first_live_day(Ledger(days, date(2026, 8, 30)), TODAY), date(2026, 9, 4))
        self.assertEqual(first_live_day(Ledger.empty(), TODAY), date(2026, 4, 20))  # the window's first day


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
        # the 19 Aug record: before the cleanup point (20 Aug), so only what a resumed transcript kept (see test_history)
        self.assertEqual(s.total_tokens, 27176)
        # 0.02495 + 0.0022 + 0.007 + 0.5 (reported) + 0.000055 + 0 + 0.0123 (reported)
        self.assertAlmostEqual(s.total_cost_usd, 0.546505, places=6)
        self.assertEqual((s.first_day, s.last_day, s.updated), (date(2026, 8, 20), TODAY, TODAY))

    def test_daily_buckets_use_dhaka_days(self):
        by_day = {d.day: d.tokens for d in self.summary().daily}
        self.assertEqual(len(by_day), 60)  # 20 Aug to 18 Oct
        self.assertEqual(by_day[date(2026, 10, 18)], 3870)  # 20 + 350 + 3500; 01:00 Dhaka is still 17 Oct in UTC
        self.assertEqual(by_day[date(2026, 10, 17)], 10300)
        self.assertEqual(by_day[date(2026, 10, 16)], 11000)
        self.assertEqual(by_day[date(2026, 10, 15)], 2006)
        self.assertEqual(by_day[date(2026, 8, 20)], 0)

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
