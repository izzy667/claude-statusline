# claude-statusline

A status line for [Claude Code](https://code.claude.com): one Python file, no
dependencies, nothing to install. Claude Code pipes a JSON payload into the
command on stdin; the script prints the line to stdout and exits.

```
483K/1M (48%) | Opus 5 E/xhigh | +140/-40 | 2h:21% 18h:14% F:6%
main ● ~1 (+44/-20) | 5h53m (⧗ 1h45m) | $67 ($11/h) | 56ms (13:51 ↻ 42m)
```

Most of what it shows cannot be read off the payload alone — session time, token
totals and per-model cost come from scanning the session transcript, and the
per-model usage windows come from the plan usage endpoint. See
[How the numbers are worked out](#how-the-numbers-are-worked-out).

## Install

Point `statusLine.command` at the script in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/statusline.py"
  }
}
```

Requires Python 3.9+. `statusline-cmd.sh` is an older shell version kept for
reference; it has the current prices but none of the features below.

## Blocks

| Block | Example | What it shows |
| --- | --- | --- |
| `context` | `483K/1M (48%)` | Context window used vs. its size. Green, yellow from 50%, red from 80%. Totals switch to `M` on the right side of the slash for 1M windows. |
| `duration` | `5h53m (⧗ 1h45m) / 7d` | Session time of the **current run**, with idle breaks over an hour removed. `⧗` is the part spent waiting for the model, always contained in the figure before it. `/ 7d` appears only on a resumed session and gives the whole transcript's total. |
| `cost` | `$67 ($11/h)` | `cost.total_cost_usd` from the payload, plus burn rate per hour of active time. Hidden below five minutes of session. `n/a` until the first API response. |
| `model` | `Opus 5 E/xhigh ⚡` | Model name, reasoning effort, `⚡` while fast mode is on. |
| `tokens` | `↓98.6M ↑340.4K C:96%` | Cumulative input (including cache reads and writes) and output tokens, and the share of input served from cache. |
| `lines` | `+140/-40` | Lines Claude added and removed this session. |
| `limits` | `2h:21% 18h:14% F:6%` | Plan usage windows. The label is the time left until that window resets, so `2h:21%` means the 5-hour window is 21% used and resets in two hours. Single letters are per-model weekly buckets (`F` = Fable). Grey, yellow from 50%, red from 80%. |
| `git` | `main ● ~1 (+44/-20)` | Branch, working-tree state, and the uncommitted line delta against `HEAD`. `○` clean, `●` dirty, then `+N` added or untracked, `~N` modified, `-N` deleted files. |
| `task` | `- Refactor the parser` | Description of the most recent `Task` tool call. Attaches to the preceding block with a dash. |
| `render` | `56ms` | How long this render took. |
| `time` | `13:51 ↻ 42m` | Wall clock, then minutes of warm prompt cache left. Turns yellow under 20 minutes, disappears when the cache is cold. |

`time` is the only block outside the default order — ask for it by name.

## Layout

The command takes one optional argument describing which blocks to render and in
what order:

```json
"command": "python3 /path/to/statusline.py context,model,limits/git,duration,cost,render+time"
```

| Syntax | Meaning |
| --- | --- |
| `a,b,c` or `a b c` | Order of blocks. Comma or space, case-insensitive. |
| `a/b` | Starts another output row. Claude Code renders each row separately. |
| `a+b` | Pairs two blocks: `b` is bracketed after `a`, in `a`'s colour. `render+time` gives `56ms (13:51 ↻ 42m)`. |

Without the argument the default order applies:
`context, duration, cost, model, tokens, lines, limits, git, task, render`.

Unknown names are dropped so one typo cannot blank the line; an argument with
nothing usable in it falls back to the default. Block names are deduplicated,
including across rows and pair members, so `git` is never forked twice. A row
whose blocks all render empty is skipped rather than left blank.

Note that the command runs through a shell. `/` is used for rows because an
unquoted `;` would end the command there, and the failing leftovers make Claude
Code drop the output entirely. `;` still works if the argument is quoted.

### Refresh

Claude Code re-runs the command when the session state changes and on each new
assistant message, but not on a timer. Blocks that count down — `time`, and the
countdown labels in `limits` — only move if you add one:

```json
"statusLine": { "…": "…", "refreshInterval": 360 }
```

Seconds, minimum 1.

## Environment variables

| Variable | Effect |
| --- | --- |
| `STATUSLINE_ESTIMATE_USAGE=1` | Adds the script's own cost estimate from the transcript: `~$71.20 ($67 $11/h)`, plus `+Fable 13%` next to the model when other model families took a share of the spend. Off by default, so nothing on the line can drift from real billing when the rate tables age. |
| `STATUSLINE_DURATION_TOTAL=1` | Makes `duration` show the whole transcript instead of the current run. The `/ 7d` suffix then disappears, since it would repeat the figure. |
| `STATUSLINE_NO_USAGE_API=1` | Disables the plan usage lookup entirely: no network, no keychain access, no per-model buckets in `limits`. |
| `STATUSLINE_DEBUG=1` | Writes the received payload to `debug_input.json` next to the script. |

Set them in the `env` block of `settings.json`.

## How the numbers are worked out

### Session and wait time

`cost.total_cost_usd` and `cost.total_duration_ms` restart on every resume, while
the transcript keeps growing across all of them — a two-month transcript would
otherwise pair one run's cost with hundreds of hours of clock. So the script
slices its own figures to the current run, using `now - total_duration_ms` as the
start.

Time is walked event by event: the gap between two consecutive entries counts as
session time when it is an hour or shorter, and as a break when it is longer.
A gap that ends in a model entry after a prompt also counts as waiting time,
which is why the bracketed figure is always contained in the one before it. To
make that sliceable after the fact, the walk also files each gap into a
15-minute bucket kept in the cache (7-day retention, the bucket the window opens
in counted pro rata).

### Cost

The payload's `total_cost_usd` is authoritative and is what the line shows. The
optional estimate is computed from the transcript instead, priced per entry from
`message.model`, because one session mixes models — subagents routinely run on a
different tier:

| Model | Input | Output |
| --- | --- | --- |
| Fable 5, Mythos 5 | $10 | $50 |
| Opus 5, 4.8, 4.7, 4.6, 4.5 | $5 | $25 |
| Opus 4.1 (retired) | $15 | $75 |
| Sonnet 5 | $2 | $10 |
| Sonnet 4.6 and earlier | $3 | $15 |
| Haiku 4.5 | $1 | $5 |

Cache multipliers on the input rate: 5-minute write 1.25x, 1-hour write 2x, read
0.1x. Web search adds $0.01 per request; `usage.speed == "fast"` prices Opus at
the fast-mode rate. Earlier billed attempts recorded in `usage.iterations` are
added at their own model's rate, and repeated `message.id` entries — a streamed
response is written to the transcript several times — are deduplicated.

Both figures are API-list prices. On a subscription they measure consumption,
not what you are charged.

### Plan limits

The payload only ever carries the 5-hour and 7-day windows. Per-model weekly
buckets exist solely in `GET /api/oauth/usage`, so the script fetches them
itself, using Claude Code's own OAuth token (credentials file, else the macOS
keychain). The token is never written anywhere; only the percentages are cached.

The request never happens in the render path. A detached `--refresh-usage`
process writes a cache shared by every session on the machine, at most once per
10 minutes and behind a lock, so five open terminals still make one request.
Polling stops after 15 minutes of silence, backs off for 30 minutes on an
authentication failure and 5 minutes on a network error, and the last good
numbers stop being shown an hour after the last success.

### Transcript scanning

Session totals come from the main transcript plus every subagent and workflow
transcript under `<transcript-dir>/<session-id>/`. Files are read incrementally:
the byte offset and running totals are cached per file, so each render only
parses what was appended since the last one. A replaced inode or a truncated
file forces a full rescan, as does a change to the cache version — which is
bumped whenever the rate tables change, so stale prices cannot survive.

Cost of a full rescan on a 691 MB transcript: about 3 seconds. Warm renders are
50–70 ms, dominated by Python interpreter startup.

## Files it writes

| Path | Purpose |
| --- | --- |
| `$TMPDIR/claude-statusline-<hash>.json` | Per-transcript scan cache: offsets, totals, per-model cost split, time buckets. |
| `$TMPDIR/claude-statusline-usage.json` | Shared plan usage cache, mode `0600`. Percentages only, never the token. |

Both are disposable; deleting them costs one slow render.

## Failure behaviour

The line degrades instead of breaking. Every section falls back to an empty
segment on bad input, a poisoned cache entry is rejected field by field, one
unparsable transcript line does not stall the offset cache, and `main()` has a
last-resort guard. A malformed payload prints nothing rather than a traceback.
