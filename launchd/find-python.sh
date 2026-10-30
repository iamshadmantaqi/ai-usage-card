#!/bin/bash
# Prints the interpreter install.sh writes into the launchd plist.
#
# macOS cannot grant Full Disk Access to the /usr/bin/python3 shim, and without it the job cannot read
# Claude Code's transcripts. The grant goes to the Command Line Tools Python.app, so launchd must start
# Python.app itself: a wrapper script that launchd starts first would be the program macOS checks.
#
# The path goes through the framework's Versions/Current link, not Versions/3.9, so a Command Line Tools
# update that moves Python to a new version folder does not leave the plist pointing at a missing binary.
# If Current is missing it prints the versioned path, and if Python.app is missing it falls back to the
# shim with a warning on stderr.
#
# AI_USAGE_CARD_BASE_PYTHON replaces /usr/bin/python3 as the interpreter asked where Python.app is (the
# tests use it).
set -euo pipefail

BASE="${AI_USAGE_CARD_BASE_PYTHON:-/usr/bin/python3}"
APP="Resources/Python.app/Contents/MacOS/Python"

PREFIX="$("$BASE" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
if [ -n "$PREFIX" ]; then
  for candidate in "$(dirname "$PREFIX")/Current/$APP" "$PREFIX/$APP"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      exit 0
    fi
  done
fi
echo "Warning: Python.app not found under '$PREFIX'; the job will use $BASE, which cannot be given Full Disk Access." >&2
echo "$BASE"
