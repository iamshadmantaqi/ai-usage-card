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
