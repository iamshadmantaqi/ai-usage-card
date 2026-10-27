# ai-usage-card

A self-updating SVG card of my AI usage over the last 30 days, shown on my [GitHub profile](https://github.com/iamshadmantaqi). It shows total tokens, the API-equivalent cost, a daily bar chart, the split by tool (Claude Code, Codex, Grok, Cursor and any other tool present) and the top six models. There is a light and a dark version, each in a wide layout (840 px) and a narrow phone layout (400 px, one column, text large enough to read on a 360 px screen).

## How it works

1. Claude Code is read straight from its transcripts (`~/.claude/projects/**/*.jsonl` and any other Claude folder T3 Code scans, subagent files included, each real folder and file once). A reply is written to a transcript several times while it streams and only the last copy has the final output count, so the copy with the most output is kept, once per message id (the request id when a line has no message id). Cache writes are priced at the 1-hour rate where the transcript says they were 1-hour writes. Only the model, the time and the token counts are taken from a line. T3 Code's own Claude records are ignored, because its cache keeps the first copy of each reply (about $1,500 short over 30 days) and prices every cache write at the 5-minute rate (about $870 short), together about 12% of Claude Code's cost.
2. Everything else (Cursor, Codex, Grok and any other tool) comes from T3 Code's local usage cache (the newest `~/.t3/userdata/usage-scan-cache-v<N>.json`, plus its Cursor and rate-table caches), which already scans those tools, so there is no parser for them here. T3 Code saves that cache when its usage page is open, so those tools are as fresh as the last visit to that page. The log gets a warning when its newest record is over 48 hours old. The 30 days always end today.
   Records are de-duplicated and priced the way T3 Code does, from its cached LiteLLM rate table. The T3 record layouts are documented in `ai_usage_card/load.py`.
3. It draws `ai-usage-light.svg`, `ai-usage-dark.svg`, `ai-usage-phone-light.svg` and `ai-usage-phone-dark.svg` and commits all four in one commit to the `usage-card` branch of the profile repo, only when the bytes of any changed.
4. A launchd job on my Mac runs this daily at 06:00. It costs no AI tokens.

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

To try it without publishing: `python3 -m ai_usage_card --out /tmp/card` writes the four SVGs there.

## It refuses to publish when

- the cache is missing, unparsable or not one of the versions checked (5 to 9; T3 writes each version to its own file, and the newest file is read), or T3 Code has started writing a version newer than 9, which someone has to check against T3's source and add to `SCAN_CACHE_VERSIONS` in `load.py`;
- the newest record from any source is older than 48 hours (a stale T3 Code cache alone only logs a warning);
- no Claude Code transcripts folder exists, a transcripts folder or file cannot be read, or T3 Code has Claude Code usage in the window while the transcripts give none;
- the privacy guard trips.

A refusal exits non-zero, logs the reason and commits nothing.

## Privacy guard

Only model names, tool names, dates and numbers reach the SVG. The renderer takes a typed `Summary` that holds nothing else, so session ids, message ids, paths, project names and chat titles never get that far. Model and tool names must also be plain names: anything with other characters, or that looks like an id, is shown as "other". Before anything is written, each of the four files is checked for `/Users/`, `/Volumes/`, `/private/`, `/home/`, `msg_`, `req_`, a UUID, a long hex id and `~`, and the run stops if any is found.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The tests use a small hand-made T3 cache and a few fixture transcripts whose expected totals are written out as plain numbers.

## Licence

MIT.
