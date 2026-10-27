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

    def test_install_script_substitutes_python(self):
        text = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("s|@PYTHON@|$PYTHON|g", text)


if __name__ == "__main__":
    unittest.main()
