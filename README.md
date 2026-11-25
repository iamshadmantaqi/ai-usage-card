# ai-usage-card

A self-updating SVG card of my AI usage over the last six months, shown on my [GitHub profile](https://github.com/iamshadmantaqi). It shows total tokens, the API-equivalent cost, a weekly bar chart, the split by tool (Claude Code, Codex, Grok, Cursor and any other tool present) and the top six models. There is a light and a dark version, each in a wide layout (840 px) and a narrow phone layout (400 px, one column, text large enough to read on a 360 px screen).

## How it works

1. Claude Code is read straight from its transcripts (`~/.claude/projects/**/*.jsonl` and any other Claude folder T3 Code scans, subagent files included, each real folder and file once). A reply is written to a transcript several times while it streams and only the last copy has the final output count, so the copy with the most output is kept, once per message id (the request id when a line has no message id). Cache writes are priced at the 1-hour rate where the transcript says they were 1-hour writes. Only the model, the time and the token counts are taken from a line. T3 Code's own Claude records are ignored, because its cache keeps the first copy of each reply (about $1,500 short over 30 days) and prices every cache write at the 5-minute rate (about $870 short), together about 12% of Claude Code's cost.
2. Everything else (Cursor, Codex, Grok and any other tool) comes from T3 Code's local usage cache (the newest `~/.t3/userdata/usage-scan-cache-v<N>.json`, plus its Cursor account cache, `usage-cursor-account-cache-v<N>.json`, and rate table), which already scans those tools, so there is no parser for them here. T3 Code rescans those tools and saves that cache only when asked for a usage summary, which its usage page does, so before reading the cache each run asks a running T3 Code for one (see "Asking T3 Code to rescan" below). When T3 Code is not running or does not answer, the cache is read as it is, as fresh as the last rescan, and the log says why. The log also gets a warning when its newest record is over 48 hours old. The window always ends today.
   Records are de-duplicated and priced the way T3 Code does, from its cached LiteLLM rate table. The T3 record layouts are documented in `ai_usage_card/load.py`.
3. Claude Code deletes its transcripts after 60 days (`cleanupPeriodDays`) and T3 Code's cache follows them, so a six-month window cannot be read from them. The first run, and every run after, saves each day that is at least 30 days old to a local history file, once, and from then on reads that day from the file. Days under 30 days old always come from the live data. A day is counted from exactly one source, never both. See "History" below.
4. It draws `ai-usage-light.svg`, `ai-usage-dark.svg`, `ai-usage-phone-light.svg` and `ai-usage-phone-dark.svg` and commits all four in one commit to the `usage-card` branch of the profile repo, only when the bytes of any changed.
5. A launchd job on my Mac runs this daily at 6:00 AM BD time. It costs no AI tokens.

## History and the window

The card reads `AI USAGE · SINCE 30 AUG 2026` (the first full day) until the history reaches back 182 days, then `AI USAGE · LAST 6 MONTHS`, a rolling window of 182 days ending today. The chart is `TOKENS PER WEEK`: weeks start on Monday in Dhaka time, the week that holds today is drawn faded because it is not finished, and a week clipped by the start of the window is drawn like any other. Dates use Asia/Dhaka.

The history is `~/Library/Application Support/ai-usage-card/history.json` (change it with `--history`). It lives outside the repo and is never published. It holds the first full day and the frozen days only:

```json
{"version": 1, "start": "2026-08-30", "days": {"2026-08-30": [{"tool": "claude", "model": "claude-opus-5-5", "tokens": 123, "cost_usd": 4.5}]}}
```

- **Freezing.** A live day at least 30 days old is added once and never changed again (cost is the API-equivalent cost at the time). A day is frozen only when both of T3 Code's caches (the scan cache and Cursor's account cache) reach past it, because T3 saves them only when its usage page is open and a stale one would freeze a day with Cursor, Codex or Grok missing. Cursor counts every account in T3's Cursor cache, and a record that appears twice counts once. Of the accounts T3 has read in the last 30 days, the one with the earliest end decides the freeze; an account T3 has not read for 30 days (a key it no longer holds or cannot read) still counts but no longer holds days back, since nothing will move its end. A day 50 days old is frozen anyway, since Claude Code's cleanup takes its transcripts at 60 (the log says so). Writes go to a temporary file that is renamed over the old one, the text is read back with the same checks first, and a run that adds nothing writes nothing. The history is saved after the card is published, so a history that cannot be written does not hold the card back.
- **First run.** Every live day at least 30 days old is frozen, and the first full day is saved as the history's `start`, so it never moves. That day is never before the cleanup point (today minus 59 days): a resumed or forked transcript keeps older messages, but other files from those days may be gone. A day on or after that point cannot have been cut, so a later first day is a real start of use. Cursor's usage comes from Cursor's servers and Codex keeps its files, so their older days do not move the start and are left out.
- **A bad file.** A missing file starts a new history. A file that is not valid JSON, has another version, holds a day, a row or a number that does not fit the layout, or holds a tool or model name that is not a plain name refuses the run (exit 1, reason in the log) and is never overwritten.
- **Trial runs.** With `--out` and no `--history` the history is read but not written, so a trial run (for example with another `--userdata`) cannot change the real file.

### Repairing Cursor's frozen days (one-off)

A day frozen while Cursor's usage was missing keeps that gap, because frozen days are never recomputed. While T3 Code's
Cursor cache still holds those days, `python3 -m ai_usage_card --repair-cursor-history --dry-run` prints, per range of days, the
Cursor tokens and cost the history holds and what the cache gives; without `--dry-run` it copies the history to
`history.json.<timestamp>.bak` next to it and replaces only the `cursor` rows of those days. Other tools' rows are untouched, days the
cache does not hold are left alone, and it refuses (writing nothing) when any Cursor account in the cache, a lapsed one included, does not cover a day it
would change, or when a day would end up with fewer Cursor tokens than the history holds. It reads the same records a freeze does. Running it twice changes nothing.

## Costs are API-equivalent

The cost is what the same tokens would cost at the public API list prices (from the LiteLLM rate table that T3 Code caches). It is not what I paid. A model with no known rate counts its tokens and adds $0.

## Install

Needs macOS, Claude Code's transcripts, T3 Code running (or its usage page opened now and then) for the other tools, the system Python 3 (`/usr/bin/python3`, standard library only) and `gh` signed in (`gh auth status`).

```sh
git clone https://github.com/iamshadmantaqi/ai-usage-card.git
cd ai-usage-card
./install.sh --kickstart
```

`install.sh` copies the code to `~/Library/Application Support/ai-usage-card/app`, installs `~/Library/LaunchAgents/com.iamshadmantaqi.ai-usage-card.plist` and, with `--kickstart`, runs the job once through launchd. Log: `~/Library/Logs/ai-usage-card.log`. Run `./install.sh` again after pulling a change.

The job runs the Command Line Tools `Python.app` (found from `/usr/bin/python3`), not the `/usr/bin/python3` shim, because macOS cannot grant Full Disk Access to the shim and the job needs it to read Claude Code's transcripts. Grant it once: System Settings > Privacy & Security > Full Disk Access > `+`, press Cmd+Shift+G, paste the path that `install.sh` prints (normally `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python`) and add it. If `Python.app` is missing, `install.sh` warns and falls back to `/usr/bin/python3`.

To try it without publishing: `python3 -m ai_usage_card --out /tmp/card` writes the four SVGs there. Add `--history /tmp/card/history.json` to see the history it would save.

## Asking T3 Code to rescan

Before it reads T3 Code's cache, each run asks the T3 Code on this Mac to rescan the last 31 days, the way its usage page does:

1. `~/.t3/bin/t3 auth session issue --scope diagnostics:read --ttl 5m --json`, T3 Code's own command for headless clients, gives a session that can only read diagnostics and expires in five minutes.
2. The card calls T3 Code's `server.getUsageSummary` over its local WebSocket (`/ws`, the port from `~/.t3/userdata/server-runtime.json`) with that session. T3 Code answers when the scan is done (about 1 to 3 seconds on a warm cache) and saves its cache as it answers, so the card waits up to 15 seconds for the cache file to change before reading it; a scan that found nothing new saves nothing, and the card then reads the cache as it is.
3. `t3 auth session revoke` removes the session at once.

The session token stays in memory for those few seconds: it is never written to a file or the log. If T3 Code is not running, its command is missing, it refuses, it answers in a form the card does not know (a T3 update can change this private interface), it sends a reply over 16 MB, or it takes over 3 minutes, the run logs one warning and reads the cache as it is. `--no-t3-refresh` skips the step. The call was checked against T3 Code 0.1.12 (November 2026); `ai_usage_card/refresh.py` has the details.

## It refuses to publish when

- the cache is missing, unparsable or older than version 5 (T3 writes each version to its own file and deletes the older ones; when an older T3 leaves two, the file saved last is read), or a version newer than 17 (the last one checked against T3's source) has files or rows this card cannot read, or rows wider than version 17's. A newer version whose files and rows still match is read with a warning in the log; check it against T3's source then and raise `SCAN_CACHE_CHECKED_THROUGH` in `load.py`;
- the newest record from any source is older than 48 hours (a stale T3 Code cache alone only logs a warning);
- no Claude Code transcripts folder exists, a transcripts folder or file cannot be read, or T3 Code has Claude Code usage in the window while the transcripts give none;
- the history file is damaged (see above);
- the privacy guard trips.

A refusal exits non-zero, logs the reason and commits nothing.

## Privacy guard

Only model names, tool names, dates and numbers reach the SVG and the history file. The renderer takes a typed `Summary` that holds nothing else, so session ids, message ids, paths, project names and chat titles never get that far. Model and tool names must also be plain names: anything with other characters, or that looks like an id, is shown as "other". Before anything is written, each of the four files is checked for `/Users/`, `/Volumes/`, `/private/`, `/home/`, `msg_`, `req_`, a UUID, a long hex id and `~`, and the run stops if any is found.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The tests use a small hand-made T3 cache and a few fixture transcripts whose expected totals are written out as plain numbers; `tests/test_history.py` covers the history and the weekly chart.

## Licence

MIT.
