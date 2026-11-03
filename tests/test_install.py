"""The launchd template takes its interpreter from install.sh, so the job can run Python.app."""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "launchd", "com.iamshadmantaqi.ai-usage-card.plist.template")
PYTHON_APP = (
    "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9"
    "/Resources/Python.app/Contents/MacOS/Python"
)


class TemplateTests(unittest.TestCase):
    def test_interpreter_is_a_placeholder(self):
        text = open(TEMPLATE).read()
        self.assertIn("<string>@PYTHON@</string>", text)
        self.assertNotIn("/usr/bin/python3", text)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
    def test_rendered_plist_is_valid_and_uses_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "job.plist")
            sed = ["sed", "-e", "s|@APP_DIR@|/tmp/app|g", "-e", "s|@LOG_FILE@|/tmp/log|g",
                   "-e", "s|@PYTHON@|%s|g" % PYTHON_APP, TEMPLATE]
            with open(out, "w") as handle:
                subprocess.run(sed, stdout=handle, check=True)
            subprocess.run(["plutil", "-lint", out], check=True, capture_output=True)
            args = subprocess.run(["plutil", "-extract", "ProgramArguments.0", "raw", "-o", "-", out],
                                  check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(args, PYTHON_APP)

    def test_install_script_shows_the_run_time_from_the_plist(self):
        text = open(os.path.join(ROOT, "install.sh")).read()
        self.assertNotIn("06:00", text)
        self.assertIn("run-time.sh", text)


@unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
class RunTimeTests(unittest.TestCase):
    SCRIPT = os.path.join(ROOT, "launchd", "run-time.sh")

    def run_time(self, zone, hour=None, minute=None):
        """What run-time.sh prints for a plist scheduled at hour:minute on a Mac set to zone."""
        text = open(TEMPLATE).read()
        if hour is not None:
            text = text.replace("<integer>6</integer>", "<integer>%d</integer>" % hour)
            text = text.replace("<integer>0</integer>", "<integer>%d</integer>" % minute)
        with tempfile.TemporaryDirectory() as tmp:
            plist = os.path.join(tmp, "job.plist")
            with open(plist, "w") as handle:
                handle.write(text)
            env = dict(os.environ, TZ=zone)
            result = subprocess.run(["bash", self.SCRIPT, plist], env=env, check=True,
                                    capture_output=True, text=True)
        return result.stdout.strip()

    def test_template_schedule_on_a_dhaka_mac(self):
        self.assertEqual(self.run_time("Asia/Dhaka"), "6:00 AM BD time")

    def test_a_mac_in_another_zone_is_converted(self):
        # 06:00 in India is 06:30 in Dhaka (both zones have no daylight saving).
        self.assertEqual(self.run_time("Asia/Kolkata"), "6:30 AM BD time")

    def test_afternoon_and_midnight_use_12_hour_form(self):
        self.assertEqual(self.run_time("Asia/Dhaka", 18, 5), "6:05 PM BD time")
        self.assertEqual(self.run_time("Asia/Dhaka", 0, 0), "12:00 AM BD time")
        self.assertEqual(self.run_time("Asia/Dhaka", 12, 30), "12:30 PM BD time")

    def test_install_script_substitutes_python(self):
        text = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("s|@PYTHON@|$PYTHON|g", text)


if __name__ == "__main__":
    unittest.main()
