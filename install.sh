#!/bin/bash
# Install the daily card job on this Mac. Safe to run again: it replaces the previous install.
#   ./install.sh            install and load the job (runs daily at 06:00)
#   ./install.sh --kickstart  also run it once now, through launchd, and show the log
set -euo pipefail

LABEL="com.iamshadmantaqi.ai-usage-card"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/ai-usage-card/app"
LOG_FILE="$HOME/Library/Logs/ai-usage-card.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# launchd jobs cannot read ~/Documents without a privacy prompt, so the job runs a copy kept
# under Application Support. Re-run this script after pulling a new version.
mkdir -p "$APP_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$PLIST")"
rm -rf "$APP_DIR/ai_usage_card"
cp -R "$HERE/ai_usage_card" "$APP_DIR/ai_usage_card"
find "$APP_DIR" -name __pycache__ -prune -exec rm -rf {} +

sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@LOG_FILE@|$LOG_FILE|g" \
  "$HERE/launchd/$LABEL.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout returns before the job is gone, and bootstrapping the same label too soon fails.
for _ in $(seq 1 50); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.1
done
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL (daily at 06:00). Log: $LOG_FILE"

if [ "${1:-}" = "--kickstart" ]; then
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "Started once through launchd; give it a few seconds, then see the log:"
  sleep 8
  tail -n 5 "$LOG_FILE"
fi
