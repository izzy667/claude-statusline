# claude-statusline

A status line for [Claude Code](https://code.claude.com): one Python file, no
dependencies, nothing to install. Claude Code pipes a JSON payload into the
command on stdin; the script prints the line to stdout and exits.

Out of the box it renders a single row:

```
487K/1M (48%) | 6h23m (⧗ 1h49m) | $69 ($11/h) | Opus 5 E/xhigh | ↓106.1M ↑362.2K C:95% | +140/-40 | 2h:22% 18h:14% F:6% | main ○ | 64ms
```

Which blocks appear, in what order, and how many rows they occupy is all
configurable — see [Layout](#layout). Split over two rows with the clock and the
cache countdown added, the same session reads:

```
487K/1M (48%) | Opus 5 E/xhigh | +140/-40 | 2h:22% 18h:14% F:6%
main ○ | 6h23m (⧗ 1h49m) | $69 ($11/h) | 64ms (14:11 ↻ 38m)
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
    "command": "python3 /path/to/statusline.py",
    "refreshInterval": 60
  }
}
```

`refreshInterval` is worth setting from the start. Claude Code only re-runs the
command when the session state changes, and every one of those moments is a
moment right after a request — so anything that counts down (the warm-cache
timer in `time`, the reset labels in `limits`) would sit at its starting value
for as long as you work and then jump. One render a minute keeps them moving and
costs about 60 ms of CPU each.

Requires Python 3.9+ and nothing else — the script imports only the standard
library.

## Blocks

| Block | Example | Colour | What it shows |
| --- | --- | --- | --- |
| `context` | `483K/1M (48%)` | green, yellow from 50%, red from 80% | Context window used vs. its size. Totals switch to `M` on the right side of the slash for 1M windows. |
| `duration` | `5h53m (⧗ 1h45m) / 7d` | magenta | Session time of the **current run**, with idle breaks over an hour removed. `⧗` is the part spent waiting for the model, always contained in the figure before it. `/ 7d` appears only on a resumed session and gives the whole transcript's total. |
| `cost` | `$67 ($11/h)` | yellow | `cost.total_cost_usd` from the payload, plus burn rate per hour of active time. Hidden below five minutes of session. `n/a` until the first API response. |
| `model` | `Opus 5 E/xhigh ⚡` | blue | Model name, reasoning effort, `⚡` while fast mode is on. |
| `tokens` | `↓98.6M ↑340.4K C:96%` | cyan | Cumulative input (including cache reads and writes) and output tokens, and the share of input served from cache. |
| `lines` | `+140/-40` | green and red | Lines Claude added and removed this session. |
| `limits` | `2h:21% 18h:14% F:6%` | grey, yellow from 50%, red from 80%, per window | Plan usage windows. The label is the time left until that window resets, so `2h:21%` means the 5-hour window is 21% used and resets in two hours. Single letters are per-model weekly buckets (`F` = Fable). |
| `git` | `main (a3f9c21) ● ~1 (+44/-20)` | cyan, delta green and red | Branch, the commit the working copy sits on, its state, and the uncommitted line delta against `HEAD`. `○` clean, `●` dirty, then `+N` added or untracked, `~N` modified, `-N` deleted files. Reads git or Plastic SCM, whichever holds the directory — see [Version control](#version-control). |
| `task` | `- Refactor the parser` | none, terminal default | Description of the most recent `Task` tool call. Attaches to the preceding block with a dash. |
| `render` | `56ms` | grey | How long this render took. |
| `time` | `13:51 ↻ 42m` | grey, countdown yellow under 20 minutes | Wall clock, then minutes of warm prompt cache left. Disappears when the cache is cold. |
| `text` | `◆ prod` | grey | A literal label of your own, supplied with `--text=`. See [Labels](#labels). |

`time` and `text` sit outside the default order — ask for them by name. Any block
can be repainted from the layout argument with `:colour`, see
[Colours](#colours).

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
| `a:colour` | Repaints one block, e.g. `time:black,context:green`. See [Colours](#colours). |

Without the argument the default order applies:
`context, duration, cost, model, tokens, lines, limits, git, task, render`.

Unknown names are dropped so one typo cannot blank the line; an argument with
nothing usable in it falls back to the default. Block names are deduplicated,
including across rows and pair members, so `git` is never forked twice. A row
whose blocks all render empty is skipped rather than left blank.

Note that the command runs through a shell. `/` is used for rows because an
unquoted `;` would end the command there, and the failing leftovers make Claude
Code drop the output entirely. `;` still works if the argument is quoted.

### Labels

`text` prints a string you supply with a `--text=` flag:

```json
"command": "python3 /path/to/statusline.py text,context,model --text='◆ prod'"
```

The flag can be repeated. Values bind to `text` blocks **by position**: the
first block takes the first value, the second the second, and so on.

```json
"command": "python3 /path/to/statusline.py text,context,model,text --text='◆ prod' --text='eu-1'"
```

Blocks with no value left render empty and are skipped; values with no block
left are ignored. Unlike every other block, `text` may appear more than once —
the rest stay deduplicated, so `git` is never forked twice.

Because the value travels as its own argument it may contain `,` `/` and `+`,
which the layout argument itself cannot. Quote it if it contains spaces:
unquoted, the shell splits `--text=◆ prod` into two arguments and only `◆`
survives. Newlines in a value are folded to spaces — rows come from the layout.

Labels render in the same grey as `render` and `time`. The value is passed
through untouched, so you can colour it yourself with an escape sequence —
written `\u001b` in JSON, since `\033` means nothing there:

```json
"--text=\u001b[0;96m◆ prod\u001b[0m"
```

Your own reset returns to the terminal default rather than to grey, which only
matters if you colour part of a label. See [Colours](#colours).

### Refresh

`refreshInterval` (seconds, minimum 1) adds a periodic re-run on top of the
event-driven ones — see [Install](#install) for why it matters. It also keeps
the wall clock in `time` current; without it the clock freezes at whatever the
last render saw.

One thing it changes on the network side: the plan usage lookup can now fire
while you are away from the keyboard. That is why polling stops after 15 minutes
of silence rather than running for as long as the terminal stays open.

## Environment variables

| Variable | Effect |
| --- | --- |
| `STATUSLINE_ESTIMATE_USAGE=1` | Adds the script's own cost estimate from the transcript: `~$71.20 ($67 $11/h)`, plus `+Fable 13%` next to the model when other model families took a share of the spend. Off by default, so nothing on the line can drift from real billing when the rate tables age. |
| `STATUSLINE_DURATION_TOTAL=1` | Makes `duration` show the whole transcript instead of the current run. The `/ 7d` suffix then disappears, since it would repeat the figure. |
| `STATUSLINE_NO_USAGE_API=1` | Disables the plan usage lookup entirely: no network, no keychain access, no per-model buckets in `limits`. |
| `STATUSLINE_DEBUG=1` | Writes the received payload to `debug_input.json` next to the script. |

Set them in the `env` block of `settings.json`.

## Colours

The line uses the basic ANSI sixteen, written `\033[0;<code>m` in the script and
`\u001b[0;<code>m` in JSON. These are palette **indices, not fixed colours**:
each renders as whatever your terminal theme calls it, so the line follows a
theme change and survives the dimming Claude Code applies to the whole row.

| Code | Constant | Used by |
| --- | --- | --- |
| 30 | `BLACK` | — |
| 31 | `RED` | `context` and `limits` at 80%+, the removed half of `lines` and of the git delta |
| 32 | `GREEN` | `context` under 50%, the added half of `lines` and of the git delta |
| 33 | `YELLOW` | `cost`, any window at 50%+, the cache countdown under 20 minutes |
| 34 | `BLUE` | `model` |
| 35 | `MAGENTA` | `duration` |
| 36 | `CYAN` | `tokens`, the branch in `git` |
| 37 | `GRAY` | `render`, `time`, `text`, windows under 50% |
| 90 | `DARK_GRAY` | — |
| 91 | `BRIGHT_RED` | — |
| 92 | `BRIGHT_GREEN` | — |
| 93 | `BRIGHT_YELLOW` | — |
| 94 | `BRIGHT_BLUE` | — |
| 95 | `BRIGHT_MAGENTA` | — |
| 96 | `BRIGHT_CYAN` | — |
| 97 | `WHITE` | — |

`task` is the one block with no colour of its own; it takes the terminal
default.

Every one has a constant in the script, and any of them can be applied to a
block straight from the layout argument by appending `:name`:

```json
"command": "python3 /path/to/statusline.py time:black,context:green,model:bright_cyan"
```

Names are case-insensitive, `grey` and `gray` both work, and a repainted block
is stripped of the colours it picked for itself first — so `context:green` stays
green at any fill level instead of turning yellow and red, and `git:white` paints
the branch and the line delta alike. In a pair the leading block's colour also
decides the brackets, so `render:cyan+time` brackets the clock in cyan.

An unrecognised colour is ignored and the block keeps its default, on the same
principle that a typo should not blank part of the line; an unrecognised block
name still drops the whole token.

The eight bright entries and black are unused by default, which makes them the
safe picks for a repaint or for a `--text=` label that should stand apart.

Attributes combine with a semicolon — `1` bold, `2` dim, `3` italic, `4`
underline, `7` inverse — and background codes are the same numbers plus ten
(`40`–`47`, `100`–`107`). Beyond the sixteen there are `\033[38;5;<0-255>m` for
the 256-colour palette and `\033[38;2;<r>;<g>;<b>m` for 24-bit colour; both work
in a label, but they pin an exact shade instead of following the theme, so a
tone picked on a dark background can disappear on a light one.

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

### Version control

The `git` block covers two backends and there is no second block to configure:
the working copy that holds the current directory decides which one answers.
Detection walks the ancestors for a marker and takes the first hit in this
order, so a directory carrying both reports Plastic:

| Order | Marker | Backend |
| --- | --- | --- |
| 1 | `.plastic` | Plastic SCM / Unity Version Control, via `cm` |
| 2 | `.git` | git — a directory (repo) or a file (worktree, submodule) |

A backend that is found but cannot answer — client not installed, server
unreachable, the call timed out — hands over to the next one rather than
blanking the block, so a fake `.plastic` beside a real repo still shows the git
branch.

Both render the same shape. Plastic reports its branch without the leading
slash and without the `@repo@server` suffix, `main/fix` for `/main/fix@x@y`; a
workspace pinned to a changeset or a label has no branch and shows `cs:12` or
`lb:v1.0`, which is the role `HEAD` plays in a detached git repo. The three
counters map straight across: `AD` and `PR` are added, `DE` and `LD` deleted,
and `CH`, `CO`, `MV`, `LM`, `CP`, `RP` modified — `CO` included, because a file
checked out and not yet edited is still something `cm ci` would commit.

The bracket after the branch is the commit the working copy sits on: a
seven-character hash for git, `(cs:211)` for Plastic. Neither costs a fork of
its own — git's comes from `status --porcelain=v2`, whose header names the
commit the v1 format leaves out, and Plastic's is already in the same XML. It
disappears where it would say nothing: a git repo with no commits yet, and a
Plastic workspace pinned to a changeset, whose branch label is that changeset
already. A label-pinned workspace keeps it, since `lb:v1.0 (cs:1)` tells you
where the label points.

The one difference is the trailing line delta, which git only has because
`git diff --numstat HEAD` exists. `cm diff` takes a changeset, label or shelve
spec; a working copy is not addressable, and rebuilding the delta would cost a
`cm cat` fork per changed file. So Plastic prints the branch and the counters
and stops there.

Cost is one fork for Plastic against git's two, but `cm` is a heavier client:
about 350 ms for `cm status --xml` where git answers in tens of milliseconds.
That is also why a single Plastic call may take 2.5 s against git's 1.5 s — a
remote Plastic server is common, and one slow answer should not cost the block.
The whole block still shares one 3 s budget, and each call is capped at what is
left of it, so a detected-but-stalled backend cannot add its timeout on top of
the next one's: the worst case is what git alone already cost.

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
