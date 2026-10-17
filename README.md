# ai-usage-card

A self-updating SVG card of my AI usage over the last 30 days, shown on my [GitHub profile](https://github.com/iamshadmantaqi). It shows total tokens, the API-equivalent cost, a daily bar chart, the split by tool (Claude Code, Codex, Grok, Cursor and any other tool present) and the top six models. There is a light and a dark version.

## How it works

1. It reads T3 Code's local usage cache (`~/.t3/userdata/usage-scan-cache-v5.json`, plus its Cursor and rate-table caches). T3 Code already scans every tool's transcripts, so there is no parser per tool here.
2. It de-duplicates records and prices them the way T3 Code does, so the card matches T3 Code's own usage view. The record layouts are documented in `ai_usage_card/load.py`.
3. It draws `ai-usage-light.svg` and `ai-usage-dark.svg` and commits them to the `usage-card` branch of the profile repo, only when the bytes changed.
4. A launchd job on my Mac runs this daily at 06:00. It costs no AI tokens.

## Costs are API-equivalent

The cost is what the same tokens would cost at the public API list prices (from the LiteLLM rate table that T3 Code caches). It is not what I paid: most of the work runs on subscriptions. A model with no known rate counts its tokens and adds $0.

## Install

Needs macOS, T3 Code running now and then, the system Python 3 (`/usr/bin/python3`, standard library only) and `gh` signed in (`gh auth status`).

```sh
git clone https://github.com/iamshadmantaqi/ai-usage-card.git
cd ai-usage-card
./install.sh --kickstart
```

`install.sh` copies the code to `~/Library/Application Support/ai-usage-card/app`, installs `~/Library/LaunchAgents/com.iamshadmantaqi.ai-usage-card.plist` and, with `--kickstart`, runs the job once through launchd. Log: `~/Library/Logs/ai-usage-card.log`. Run `./install.sh` again after pulling a change.

To try it without publishing: `python3 -m ai_usage_card --out /tmp/card` writes the two SVGs there.

## It refuses to publish when

- the cache is missing, unparsable or not version 5;
- the newest record is older than 48 hours (T3 Code has not been running);
- the privacy guard trips.

A refusal exits non-zero, logs the reason and commits nothing.

## Privacy guard

Only model names, tool names, dates and numbers reach the SVG. The renderer takes a typed `Summary` that holds nothing else, so session ids, message ids, paths, project names and chat titles never get that far. Model and tool names must also be plain names: anything with other characters, or that looks like an id, is shown as "other". Before anything is written, the output is checked for `/Users/`, `msg_`, `req_`, a UUID and `~`, and the run stops if any is found.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The tests use a small hand-made cache whose expected totals are written out as plain numbers.

## Licence

MIT.
