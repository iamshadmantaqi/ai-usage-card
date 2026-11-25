"""launchd starts Python.app itself, through the framework's Versions/Current link."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "launchd", "com.iamshadmantaqi.ai-usage-card.plist.template")
FIND_PYTHON = os.path.join(ROOT, "launchd", "find-python.sh")
APP = os.path.join("Resources", "Python.app", "Contents", "MacOS", "Python")
PYTHON_APP = (
    "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/Current/" + APP
)


def make_script(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class TemplateTests(unittest.TestCase):
    def test_interpreter_is_a_placeholder_launchd_starts_directly(self):
        # A wrapper script as the first program would be what macOS checks for Full Disk Access.
        text = open(TEMPLATE).read()
        self.assertIn("<string>@PYTHON@</string>", text)
        self.assertNotIn("/bin/bash", text)
        self.assertNotIn("/usr/bin/python3", text)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
    def test_rendered_plist_is_valid_and_starts_python_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "job.plist")
            sed = ["sed", "-e", "s|@APP_DIR@|/tmp/app|g", "-e", "s|@LOG_FILE@|/tmp/log|g",
                   "-e", "s|@PYTHON@|%s|g" % PYTHON_APP, TEMPLATE]
            with open(out, "w") as handle:
                subprocess.run(sed, stdout=handle, check=True)
            subprocess.run(["plutil", "-lint", out], check=True, capture_output=True)
            with open(out) as handle:
                self.assertNotIn("@", handle.read().split("<plist", 1)[1])
            args = [subprocess.run(["plutil", "-extract", "ProgramArguments.%d" % i, "raw", "-o", "-", out],
                                   check=True, capture_output=True, text=True).stdout.strip()
                    for i in range(3)]
            self.assertEqual(args, [PYTHON_APP, "-m", "ai_usage_card"])

    def test_install_script_takes_python_from_find_python(self):
        text = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn('PYTHON="$("$HERE/launchd/find-python.sh")"', text)
        self.assertIn("s|@PYTHON@|$PYTHON|g", text)

    def test_install_script_shows_the_run_time_from_the_plist(self):
        text = open(os.path.join(ROOT, "install.sh")).read()
        self.assertNotIn("06:00", text)
        self.assertIn("run-time.sh", text)

    def test_a_failing_run_time_does_not_stop_the_install(self):
        # install.sh runs under set -e; the run time only goes into the closing message.
        line = [l for l in open(os.path.join(ROOT, "install.sh")) if l.startswith("RUN_TIME=")]
        self.assertEqual(len(line), 1)
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "launchd"))
            with open(os.path.join(tmp, "launchd", "run-time.sh"), "w") as handle:
                handle.write("exit 1\n")
            script = 'set -euo pipefail\nHERE="$1"\nPLIST=/tmp/job.plist\n%secho "$RUN_TIME"\n' % line[0]
            result = subprocess.run(["bash", "-c", script, "bash", tmp], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "the time set in /tmp/job.plist")


@unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
class RunTimeTests(unittest.TestCase):
    SCRIPT = os.path.join(ROOT, "launchd", "run-time.sh")

    def run_time(self, zone, hour=6, minute=0, **env):
        """What run-time.sh prints for a plist scheduled at hour:minute on a Mac set to zone."""
        text = open(TEMPLATE).read()
        schedule = "<key>Hour</key>\n    <integer>%d</integer>\n    <key>Minute</key>\n    <integer>%d</integer>"
        self.assertIn(schedule % (6, 0), text)
        text = text.replace(schedule % (6, 0), schedule % (hour, minute))
        with tempfile.TemporaryDirectory() as tmp:
            plist = os.path.join(tmp, "job.plist")
            with open(plist, "w") as handle:
                handle.write(text)
            env = dict(os.environ, TZ=zone, **env)
            result = subprocess.run(["bash", self.SCRIPT, plist], env=env, check=True,
                                    capture_output=True, text=True)
        return result.stdout.strip()

    def test_template_schedule_on_a_dhaka_mac(self):
        self.assertEqual(self.run_time("Asia/Dhaka"), "6:00 AM BD time")

    def test_a_mac_in_another_zone_is_converted(self):
        # 06:00 in India is 06:30 in Dhaka, and in Nepal 06:15 (none has daylight saving).
        self.assertEqual(self.run_time("Asia/Kolkata"), "6:30 AM BD time")
        self.assertEqual(self.run_time("Asia/Kathmandu"), "6:15 AM BD time")

    def test_afternoon_and_midnight_use_12_hour_form(self):
        self.assertEqual(self.run_time("Asia/Dhaka", 18, 5), "6:05 PM BD time")
        self.assertEqual(self.run_time("Asia/Dhaka", 0, 0), "12:00 AM BD time")
        self.assertEqual(self.run_time("Asia/Dhaka", 0, 5), "12:05 AM BD time")
        self.assertEqual(self.run_time("Asia/Dhaka", 12, 30), "12:30 PM BD time")

    def test_am_and_pm_whatever_the_locale(self):
        for locale in ("de_DE.UTF-8", "ja_JP.UTF-8"):
            self.assertEqual(self.run_time("Asia/Dhaka", 18, 0, LC_ALL=locale), "6:00 PM BD time")


class FindPythonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.versions = os.path.join(self.tmp.name, "Versions")

    def find(self, prefix):
        base = os.path.join(self.tmp.name, "bin", "python3")
        make_script(base, "echo '%s'\n" % prefix)
        env = dict(os.environ, AI_USAGE_CARD_BASE_PYTHON=base)
        return base, subprocess.run(["bash", FIND_PYTHON], env=env, capture_output=True, text=True)

    def test_prefers_the_current_link_so_an_update_keeps_the_path_valid(self):
        make_script(os.path.join(self.versions, "3.9", APP), "echo old\n")
        os.symlink("3.9", os.path.join(self.versions, "Current"))
        _, result = self.find(os.path.join(self.versions, "3.9"))
        current = os.path.join(self.versions, "Current", APP)
        self.assertEqual(result.stdout.strip(), current)
        # A Command Line Tools update moves Python to a new version folder and repoints Current.
        shutil.rmtree(os.path.join(self.versions, "3.9"))
        os.remove(os.path.join(self.versions, "Current"))
        make_script(os.path.join(self.versions, "3.12", APP), "echo new\n")
        os.symlink("3.12", os.path.join(self.versions, "Current"))
        self.assertEqual(subprocess.run([current], capture_output=True, text=True).stdout.strip(), "new")

    def test_uses_the_versioned_path_without_a_current_link(self):
        prefix = os.path.join(self.versions, "3.9")
        make_script(os.path.join(prefix, APP), "echo app\n")
        _, result = self.find(prefix)
        self.assertEqual(result.stdout.strip(), os.path.join(prefix, APP))
        self.assertEqual(result.stderr, "")

    def test_warns_when_the_python_it_picks_is_too_old(self):
        prefix = os.path.join(self.versions, "3.8")
        make_script(os.path.join(prefix, APP), "exit 1\n")
        _, result = self.find(prefix)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), os.path.join(prefix, APP))
        self.assertIn("older than Python 3.9", result.stderr)

    def test_falls_back_to_the_shim_with_a_warning(self):
        base, result = self.find(os.path.join(self.tmp.name, "missing"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), base)
        self.assertIn("Python.app not found", result.stderr)

    def test_real_system_python_resolves_to_an_existing_binary(self):
        if not os.path.exists("/usr/bin/python3"):
            self.skipTest("no system Python")
        env = {k: v for k, v in os.environ.items() if k != "AI_USAGE_CARD_BASE_PYTHON"}
        out = subprocess.run(["bash", FIND_PYTHON], env=env, capture_output=True, text=True)
        self.assertTrue(os.access(out.stdout.strip(), os.X_OK), out)


if __name__ == "__main__":
    unittest.main()
