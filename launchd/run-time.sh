#!/bin/bash
# Print the daily run time of a launchd plist as 12-hour Bangladesh time, for example "6:00 AM BD time".
# launchd reads the schedule on the Mac's own clock, so convert it from the Mac's zone to Asia/Dhaka.
set -euo pipefail

plist="${1:?usage: run-time.sh <plist>}"
hour="$(plutil -extract StartCalendarInterval.Hour raw -o - "$plist")"
minute="$(plutil -extract StartCalendarInterval.Minute raw -o - "$plist")"
local_time="$(printf '%02d:%02d:00' "$hour" "$minute")"
epoch="$(date -j -f '%H:%M:%S' "$local_time" +%s)"
# LC_ALL=C: %p is AM or PM only in the C locale; German gives an empty string, Japanese 午前.
TZ=Asia/Dhaka LC_ALL=C date -r "$epoch" '+%-I:%M %p BD time'
