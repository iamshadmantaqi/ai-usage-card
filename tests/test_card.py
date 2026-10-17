from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from ai_usage_card import cli
from ai_usage_card.load import CacheError, load_records
from ai_usage_card.pricing import load_rates, price
from ai_usage_card.privacy import PrivacyError, check_privacy
from ai_usage_card.render import DARK, LIGHT, fmt_cost, fmt_tokens, render
from ai_usage_card.summarise import summarise
from tests import fixtures as fx

TODAY = date(2026, 10, 18)


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.userdata = fx.write_userdata(self.tmp / "userdata")

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
        self.assertEqual((record.model, record.rate_model), ("cursor-auto", "default"))
        # priced through the original name: 3000 * 3e-6 + 500 * 1e-5
        self.assertAlmostEqual(price(rates, record), 0.014, places=9)
        self.assertEqual(price(rates, record), price(rates, record.__class__(**dict(record.__dict__, model="default"))))
        labels = [m.label for m in self.summary().models]
        self.assertIn("cursor-auto", labels)
        self.assertNotIn("default", labels)


class PricingTests(Case):
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

    def test_bare_family_names_are_never_priced(self):
        rates = load_rates(self.userdata)
        record = next(r for r in load_records(self.userdata) if r.model == "claude-sonnet-5-5")
        self.assertEqual(price(rates, record.__class__(**dict(record.__dict__, model="sonnet", rate_model=None))), 0.0)


class SummaryTests(Case):
    def test_thirty_day_totals(self):
        s = self.summary()
        # 10000 + 300 + 11000 + 20 (claude) + 350 (codex) + 2006 (grok) + 3500 (cursor); synthetic and the 1 Sep record are out
        self.assertEqual(s.total_tokens, 27176)
        # 0.02495 + 0.0022 + 0.007 + 0.5 (reported) + 0.000055 + 0 + 0.0123 (reported)
        self.assertAlmostEqual(s.total_cost_usd, 0.546505, places=6)
        self.assertEqual((s.first_day, s.last_day, s.updated), (date(2026, 9, 19), TODAY, TODAY))

    def test_daily_buckets_use_dhaka_days(self):
        by_day = {d.day: d.tokens for d in self.summary().daily}
        self.assertEqual(len(by_day), 30)
        self.assertEqual(by_day[date(2026, 10, 18)], 3870)  # 20 + 350 + 3500; 01:00 Dhaka is still 17 Oct in UTC
        self.assertEqual(by_day[date(2026, 10, 17)], 10300)
        self.assertEqual(by_day[date(2026, 10, 16)], 11000)
        self.assertEqual(by_day[date(2026, 10, 15)], 2006)
        self.assertEqual(by_day[date(2026, 9, 19)], 0)

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

    def test_a_real_zero_is_drawn_as_zero(self):
        empty = summarise([], load_rates(self.userdata), TODAY)
        svg = render(empty, DARK)
        ET.fromstring(svg)
        self.assertIn(">0<", svg)
        self.assertIn(">$0<", svg)

    def test_number_formats(self):
        self.assertEqual([fmt_tokens(n) for n in (0, 999, 1234, 12345678, 123456789, 999960, 1_234_567_890)],
                         ["0", "999", "1.2K", "12.3M", "123M", "1.0M", "1.2B"])
        self.assertEqual([fmt_cost(c) for c in (0, 0.0123, 99.99, 1234.5)], ["$0", "$0.01", "$99.99", "$1,234"])

    def test_privacy_guard_refuses_paths_and_ids(self):
        for bad in ("/Users/me/x", "msg_01ABC", "req_01ABC", "123e4567-e89b-12d3-a456-426614174000", "~/notes"):
            with self.assertRaises(PrivacyError, msg=bad):
                check_privacy("<svg>%s</svg>" % bad)
        check_privacy(render(self.summary(), DARK))

    def test_a_model_named_like_an_id_is_shown_as_other(self):
        scan = fx.scan_cache()
        scan["models"] = scan["models"][:]
        scan["models"][4] = "123e4567-e89b-12d3-a456-426614174000"
        fx.write_userdata(self.userdata, scan=scan)
        for theme in (LIGHT, DARK):
            check_privacy(render(self.summary(), theme))
        self.assertNotIn("123e4567", render(self.summary(), DARK))


class CliTests(Case):
    def run_cli(self, *extra, now=fx.NOW):
        args = ["--userdata", str(self.userdata), "--log", str(self.tmp / "card.log")] + list(extra)
        return cli.main(args, now=now)

    def test_writes_both_files_to_out(self):
        self.assertEqual(self.run_cli("--out", str(self.tmp / "out")), 0)
        self.assertEqual(sorted(p.name for p in (self.tmp / "out").iterdir()), ["ai-usage-dark.svg", "ai-usage-light.svg"])

    def test_refuses_when_the_newest_record_is_older_than_48_hours(self):
        out = self.tmp / "out"
        self.assertEqual(self.run_cli("--out", str(out), now=fx.NOW + timedelta(hours=49)), 1)
        self.assertFalse(out.exists())
        self.assertIn("T3 Code is not running", (self.tmp / "card.log").read_text())

    def test_refuses_when_the_privacy_guard_trips(self):
        out = self.tmp / "out"
        with mock.patch.object(cli, "render", return_value="<svg>/Users/someone</svg>"):
            self.assertEqual(self.run_cli("--out", str(out)), 1)
        self.assertFalse(out.exists())
        self.assertIn("PrivacyError", (self.tmp / "card.log").read_text())

    def test_refuses_an_unsupported_cache_version(self):
        scan = fx.scan_cache()
        scan["version"] = 6
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
            self.assertEqual(git("ls-tree", "--name-only", "usage-card").split("\n"), ["ai-usage-dark.svg", "ai-usage-light.svg"])
            self.assertEqual(git("log", "-1", "--format=%s", "usage-card"), "Update AI usage card")
            self.assertEqual(git("rev-list", "--max-parents=0", "usage-card"), git("rev-parse", "usage-card"))  # the commit is the orphan root
            # a new record changes the card: exactly one more commit
            scan = fx.scan_cache()
            scan["files"]["/f/f.jsonl"] = fx.entry("claude", [fx.row(fx.ms(10, 18, 3), "claude-sonnet-5-5", 0, 1, 0, 0, 1, "msg_z:req_z")])
            fx.write_userdata(self.userdata, scan=scan)
            self.assertEqual(self.run_cli(*publish_args), 0)
            self.assertEqual(git("rev-list", "--count", "usage-card"), "2")

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
