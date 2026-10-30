# ai-usage-card

A self-updating SVG card of my AI usage over the last six months, shown on my [GitHub profile](https://github.com/iamshadmantaqi). It shows total tokens, the API-equivalent cost, a weekly bar chart, the split by tool (Claude Code, Codex, Grok, Cursor and any other tool present) and the top six models. There is a light and a dark version, each in a wide layout (840 px) and a narrow phone layout (400 px, one column, text large enough to read on a 360 px screen).

## How it works

1. Claude Code is read straight from its transcripts (`~/.claude/projects/**/*.jsonl` and any other Claude folder T3 Code scans, subagent files included, each real folder and file once). A reply is written to a transcript several times while it streams and only the last copy has the final output count, so the copy with the most output is kept, once per message id (the request id when a line has no message id). Cache writes are priced at the 1-hour rate where the transcript says they were 1-hour writes. Only the model, the time and the token counts are taken from a line. T3 Code's own Claude records are ignored, because its cache keeps the first copy of each reply (about $1,500 short over 30 days) and prices every cache write at the 5-minute rate (about $870 short), together about 12% of Claude Code's cost.
2. Everything else (Cursor, Codex, Grok and any other tool) comes from T3 Code's local usage cache (the newest `~/.t3/userdata/usage-scan-cache-v<N>.json`, plus its Cursor and rate-table caches), which already scans those tools, so there is no parser for them here. T3 Code saves that cache when its usage page is open, so those tools are as fresh as the last visit to that page. The log gets a warning when its newest record is over 48 hours old. The window always ends today.
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

- **Freezing.** A live day at least 30 days old is added once and never changed again (cost is the API-equivalent cost at the time). A day is frozen only when both of T3 Code's caches (the scan cache and Cursor's account cache) reach past it, because T3 saves them only when its usage page is open and a stale one would freeze a day with Cursor, Codex or Grok missing. A day 50 days old is frozen anyway, since Claude Code's cleanup takes its transcripts at 60 (the log says so). Writes go to a temporary file that is renamed over the old one, the text is read back with the same checks first, and a run that adds nothing writes nothing. The history is saved after the card is published, so a history that cannot be written does not hold the card back.
- **First run.** Every live day at least 30 days old is frozen, and the first full day is saved as the history's `start`, so it never moves. That day is never before the cleanup point (today minus 59 days): a resumed or forked transcript keeps older messages, but other files from those days may be gone. A day on or after that point cannot have been cut, so a later first day is a real start of use. Cursor's usage comes from Cursor's servers and Codex keeps its files, so their older days do not move the start and are left out.
- **A bad file.** A missing file starts a new history. A file that is not valid JSON, has another version, holds a day, a row or a number that does not fit the layout, or holds a tool or model name that is not a plain name refuses the run (exit 1, reason in the log) and is never overwritten.
- **Trial runs.** With `--out` and no `--history` the history is read but not written, so a trial run (for example with another `--userdata`) cannot change the real file.

## Costs are API-equivalent

The cost is what the same tokens would cost at the public API list prices (from the LiteLLM rate table that T3 Code caches). It is not what I paid. A model with no known rate counts its tokens and adds $0.

## Install

Needs macOS, Claude Code's transcripts, T3 Code's usage page opened now and then (for the other tools), the system Python 3 (`/usr/bin/python3`, standard library only) and `gh` signed in (`gh auth status`).

```sh
git clone https://github.com/iamshadmantaqi/ai-usage-card.git
cd ai-usage-card
./install.sh --kickstart
```

`install.sh` copies the code to `~/Library/Application Support/ai-usage-card/app`, installs `~/Library/LaunchAgents/com.iamshadmantaqi.ai-usage-card.plist` and, with `--kickstart`, runs the job once through launchd. Log: `~/Library/Logs/ai-usage-card.log`. Run `./install.sh` again after pulling a change.

launchd starts the Command Line Tools `Python.app` directly (`install.sh` finds it from `/usr/bin/python3`), not the `/usr/bin/python3` shim or a wrapper script, because macOS cannot grant Full Disk Access to the shim and the job needs it to read Claude Code's transcripts. The plist names it through the framework's `Versions/Current` link, so a Command Line Tools update that moves Python to a new version folder does not leave the job pointing at a missing binary; `launchd/find-python.sh` prints the path, falling back to the versioned path when there is no `Current` link. Grant it once: System Settings > Privacy & Security > Full Disk Access > `+`, press Cmd+Shift+G, paste the path that `install.sh` prints (normally `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/Current/Resources/Python.app/Contents/MacOS/Python`) and add it. If a Command Line Tools update brings a new `Python.app` and the log says macOS is blocking the job, grant it again. If `Python.app` is missing, `install.sh` warns and falls back to `/usr/bin/python3`.

To try it without publishing: `python3 -m ai_usage_card --out /tmp/card` writes the four SVGs there. Add `--history /tmp/card/history.json` to see the history it would save.

## It refuses to publish when

- the cache is missing, unparsable or not one of the versions checked (5 to 9; T3 writes each version to its own file, and the newest file is read), or T3 Code has started writing a version newer than 9, which someone has to check against T3's source and add to `SCAN_CACHE_VERSIONS` in `load.py`;
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
