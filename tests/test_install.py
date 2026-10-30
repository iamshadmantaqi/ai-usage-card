"""The launchd job runs a wrapper that picks the interpreter when it starts, not when it is installed."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "launchd", "com.iamshadmantaqi.ai-usage-card.plist.template")
WRAPPER = os.path.join(ROOT, "launchd", "run-card.sh")


def make_script(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("#!/bin/bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class TemplateTests(unittest.TestCase):
    def test_plist_names_no_interpreter_path(self):
        text = open(TEMPLATE).read()
        self.assertNotIn("Python.app", text)
        self.assertNotIn("/usr/bin/python3", text)
        self.assertNotIn("@PYTHON@", text)
        self.assertIn("<string>@APP_DIR@/run-card.sh</string>", text)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
    def test_rendered_plist_is_valid_and_runs_the_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "job.plist")
            sed = ["sed", "-e", "s|@APP_DIR@|/tmp/app|g", "-e", "s|@LOG_FILE@|/tmp/log|g", TEMPLATE]
            with open(out, "w") as handle:
                subprocess.run(sed, stdout=handle, check=True)
            subprocess.run(["plutil", "-lint", out], check=True, capture_output=True)
            args = [subprocess.run(["plutil", "-extract", "ProgramArguments.%d" % i, "raw", "-o", "-", out],
                                   check=True, capture_output=True, text=True).stdout.strip()
                    for i in range(2)]
            self.assertEqual(args, ["/bin/bash", "/tmp/app/run-card.sh"])

    def test_install_script_copies_the_wrapper(self):
        text = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn('cp "$HERE/launchd/run-card.sh" "$APP_DIR/run-card.sh"', text)
        self.assertNotIn("@PYTHON@", text)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_wrapper(self, base, *args):
        env = dict(os.environ, AI_USAGE_CARD_BASE_PYTHON=base)
        return subprocess.run(["bash", WRAPPER, *args], env=env, capture_output=True, text=True)

    def fake_base(self, prefix):
        base = os.path.join(self.tmp.name, "bin", "python3")
        make_script(base, "echo '%s'\n" % prefix)
        return base

    def test_uses_python_app_found_at_run_time(self):
        prefix = os.path.join(self.tmp.name, "Versions", "9.9")
        app = os.path.join(prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
        make_script(app, 'echo "app $@"\n')
        result = self.run_wrapper(self.fake_base(prefix), "--print-python")
        self.assertEqual(result.stdout.strip(), app)
        result = self.run_wrapper(self.fake_base(prefix))
        self.assertEqual(result.stdout.strip(), "app -m ai_usage_card")

    def test_follows_a_moved_python_app(self):
        # The same wrapper, run again after an update moved Python.app, finds the new place.
        old = os.path.join(self.tmp.name, "Versions", "3.9")
        new = os.path.join(self.tmp.name, "Versions", "3.10")
        tail = os.path.join("Resources", "Python.app", "Contents", "MacOS", "Python")
        make_script(os.path.join(old, tail), "echo old\n")
        self.assertEqual(self.run_wrapper(self.fake_base(old), "--print-python").stdout.strip(),
                         os.path.join(old, tail))
        shutil.rmtree(old)
        make_script(os.path.join(new, tail), "echo new\n")
        self.assertEqual(self.run_wrapper(self.fake_base(new), "--print-python").stdout.strip(),
                         os.path.join(new, tail))

    def test_falls_back_to_the_base_python_when_python_app_is_missing(self):
        base = self.fake_base(os.path.join(self.tmp.name, "missing"))
        result = self.run_wrapper(base, "--print-python")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), base)
        self.assertIn("Python.app not found", result.stderr)

    def test_real_system_python_resolves_to_an_existing_binary(self):
        if not os.path.exists("/usr/bin/python3"):
            self.skipTest("no system Python")
        env = {k: v for k, v in os.environ.items() if k != "AI_USAGE_CARD_BASE_PYTHON"}
        out = subprocess.run(["bash", WRAPPER, "--print-python"], env=env, capture_output=True, text=True)
        self.assertTrue(os.access(out.stdout.strip(), os.X_OK), out)


if __name__ == "__main__":
    unittest.main()
