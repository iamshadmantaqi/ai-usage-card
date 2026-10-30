#!/bin/bash
# What the launchd job runs. It picks the interpreter each time it starts, so a Command Line Tools
# update that moves Python.app cannot leave the job pointing at a missing binary.
#   run-card.sh [args]          run the card with the Command Line Tools Python.app
#   run-card.sh --print-python  print the interpreter it would run, and say so on stderr if it is a fallback
# AI_USAGE_CARD_BASE_PYTHON replaces /usr/bin/python3 as the interpreter that is asked where Python.app is
# (the tests use it).
set -euo pipefail

BASE="${AI_USAGE_CARD_BASE_PYTHON:-/usr/bin/python3}"

# macOS cannot grant Full Disk Access to the /usr/bin/python3 shim, and without it the job cannot read
# Claude Code's transcripts. The grant goes to the Command Line Tools Python.app, so the job runs that.
PREFIX="$("$BASE" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
PYTHON="$PREFIX/Resources/Python.app/Contents/MacOS/Python"
if [ -z "$PREFIX" ] || [ ! -x "$PYTHON" ]; then
  echo "Warning: Python.app not found under '$PREFIX'; using $BASE, which cannot be given Full Disk Access." >&2
  PYTHON="$BASE"
fi

if [ "${1:-}" = "--print-python" ]; then
  echo "$PYTHON"
  exit 0
fi

echo "Running with $PYTHON" >&2
exec "$PYTHON" -m ai_usage_card "$@"
