#!/usr/bin/env python3
"""Claude Code status line — cross-platform (Windows/macOS/Linux).

Reads the statusline JSON payload from stdin and prints a single ANSI line.
Contract: never print a traceback — every section degrades to an empty
segment on bad input, and main() has a last-resort guard.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime

START_TIME = time.perf_counter()  # for the trailing render-time segment

RESET = "\033[0m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BLUE = "\033[0;34m"
MAGENTA = "\033[0;35m"
CYAN = "\033[0;36m"
GRAY = "\033[0;37m"

# Tiers: (match key, family label, (input, output) rate per MTok).
# Cache multipliers on the input rate: 5m write 1.25x, 1h write 2x, read 0.1x.
# Verified against platform.claude.com/docs/en/about-claude/pricing, 2026-09-01.
# Display-name families — fallback for transcript entries without a model id.
# More specific keys first — "Opus 4.1" must win over "Opus".
BASE_TIERS = (
    ("Opus 4.1", "Opus", (15, 75)),  # retired 2026-08-05; kept for old transcripts
    ("Fable", "Fable", (10, 50)),
    ("Mythos", "Mythos", (10, 50)),
    ("Opus", "Opus", (5, 25)),
    ("Sonnet 5", "Sonnet", (2, 10)),  # introductory $2/$10 is now the standard price
    ("Sonnet", "Sonnet", (3, 15)),  # Sonnet 4.6 and earlier
    ("Haiku", "Haiku", (1, 5)),
)
# Model-id substrings (lowercase, most specific first) — per-entry pricing;
# one session mixes models (subagents often run on a different tier).
MODEL_TIERS = (
    ("opus-4-1", "Opus", (15, 75)),
    ("fable", "Fable", (10, 50)),
    ("mythos", "Mythos", (10, 50)),
    ("opus", "Opus", (5, 25)),
    ("haiku", "Haiku", (1, 5)),
    ("sonnet-5", "Sonnet", (2, 10)),
    ("sonnet", "Sonnet", (3, 15)),
)
DEFAULT_TIER = ("?", (3, 15))
# Fast mode (/fast) bills Opus 5 and Opus 4.8 at Fable-tier rates across the
# whole request; the API echoes the speed it actually served in usage.speed.
FAST_RATES = (10, 50)

EFFORT_ABBREV = {"low": "low", "medium": "med", "high": "high", "max": "max", "auto": "auto"}

# Bump on schema OR pricing changes — cached usd values depend on the rate tables
CACHE_VERSION = 9
WEB_SEARCH_USD = 0.01  # $10 per 1000 searches
USAGE_KEYS = ("in", "cc", "cr", "out")
BURN_RATE_MIN_S = 300  # hide burn rate for sessions under 5 minutes (too noisy)
IDLE_GAP_S = 3600  # response→next-prompt breaks longer than this don't count as session time
RECENT_IDS_MAX = 16  # dedup window: streamed duplicates are near-consecutive in practice
# Session time is bucketed so it can be sliced to the current run: cost and
# total_duration_ms reset on every resume, the transcript does not.
TIME_BUCKET_S = 900
TIME_BUCKET_KEEP_S = 7 * 86400
SPAN_MIN_DELTA_S = 300  # below this the transcript total just repeats the run
WAIT_MARK = "⧗ "  # marks the waiting time so two clocks side by side stay readable
MODEL_MIX_MAX = 2  # extra model families shown next to the main-loop model
MODEL_MIX_MIN_PCT = 1  # below this share of session cost a family is noise

# Per-model weekly windows (Fable) live only in the plan usage endpoint — the
# statusline payload carries five_hour/seven_day and nothing else. A detached
# refresher does the GET; the render path only ever reads the cache file.
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
USAGE_CACHE_VERSION = 1
USAGE_CACHE_TTL_S = 600  # at most one GET per 10 min, shared by every session
USAGE_TIMEOUT_S = 8
USAGE_LOCK_S = 60  # a refresher killed mid-flight must not block the next one
USAGE_RETRY_NET_S = 300  # transient failure: back off before asking again
USAGE_RETRY_AUTH_S = 1800  # no/expired token: Claude Code owns the refresh
USAGE_STALE_S = 3600  # past this the last good numbers stop being shown


def as_number(value) -> float | None:
    """Coerce a payload value to a finite float; None for anything else.

    Rejects NaN/Infinity (JSON literals, "nan"/"inf" strings, overflowing ints) —
    a non-finite value anywhere downstream would crash int() or poison display.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def parse_ts(value) -> float:
    """ISO-8601 transcript timestamp → epoch seconds; 0.0 when unparsable."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        return f"{val:.1f}M".replace(".0M", "M")
    if tokens >= 1_000:
        val = tokens / 1_000
        return f"{val:.1f}K".replace(".0K", "K")
    return str(tokens)


def get_file_creation_time(path: str) -> float:
    """Return file creation (birth) time in seconds since epoch, or 0."""
    try:
        st = os.stat(path)
        # Windows & macOS expose st_birthtime / st_ctime as creation time
        # On Linux st_ctime is metadata-change time, but birthtime may be available
        birth = getattr(st, "st_birthtime", None)
        if birth is not None:
            return birth
        # Fallback: on Windows os.stat().st_ctime IS creation time
        if sys.platform == "win32":
            return st.st_ctime
        # On Linux, st_ctime is inode change time — use mtime as rough fallback
        return st.st_mtime
    except OSError:
        return 0.0


# --- Model & effort ---

def read_settings_effort() -> str:
    """Fallback for payloads without effort.level (older Claude Code versions)."""
    try:
        settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        with open(settings_path, "r", encoding="utf-8") as sf:
            settings = json.load(sf)
        if not isinstance(settings, dict):
            return ""
        raw = settings.get("effortLevel")
        return raw if isinstance(raw, str) else ""
    except Exception:
        return ""


def get_effort(data: dict) -> str:
    # Payload value reflects live /effort changes; settings.json goes stale mid-session
    effort_obj = data.get("effort")
    level = effort_obj.get("level") if isinstance(effort_obj, dict) else None
    if not isinstance(level, str) or not level:
        level = read_settings_effort()
    if not level:
        return ""
    level = level.lower()
    return EFFORT_ABBREV.get(level, level)


def model_segment(data: dict) -> str:
    model_obj = data.get("model")
    model = model_obj.get("display_name") if isinstance(model_obj, dict) else None
    if not isinstance(model, str) or not model:
        model = "Unknown"
    # Drop the "(1M context)" suffix — the context block already shows the
    # window size, so repeating it here only costs width. Matches any size.
    if model.endswith(" context)"):
        cut = model.rfind(" (")
        if cut > 0:
            model = model[:cut]
    effort = get_effort(data)
    if effort:
        model = f"{model} E/{effort}"
    if data.get("fast_mode") is True:
        model = f"{model} ⚡"  # /fast doubles the Opus rate to $10/$50 per MTok
    return model


def estimates_enabled() -> bool:
    """Own price-table estimates are opt-in — STATUSLINE_ESTIMATE_USAGE=1.

    Off by default the line carries only figures Claude Code itself reports, so
    nothing shown can drift from real billing when the rate tables age.
    """
    return os.environ.get("STATUSLINE_ESTIMATE_USAGE") == "1"


def model_mix_segment(current: str, stats: dict) -> str:
    """Cost share of families the main loop is NOT running on.

    Subagents, workflows and mid-session /model switches routinely bill on a
    different tier — Fable at 2x Opus — and the payload only ever names the
    main-loop model, so that spend is otherwise invisible.
    """
    total = stats["usd"]
    if total <= 0 or not estimates_enabled():
        return ""
    others = sorted(
        ((label, usd) for label, usd in stats["models"].items() if label != current),
        key=lambda item: -item[1],
    )
    parts = [
        f"+{label} {usd * 100 / total:.0f}%"
        for label, usd in others[:MODEL_MIX_MAX]
        if usd * 100 / total >= MODEL_MIX_MIN_PCT
    ]
    return f" {' '.join(parts)}" if parts else ""


# --- Context window ---

def context_color(pct: float) -> str:
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def format_context(current: int, size: int) -> tuple:
    """Used side always in K; only the total switches to M at a 1M window.

    "341K/1M" beats both "341K/1000K" and "0.34M/1M" — the used value is the one
    that changes every render, so it stays in the shortest unit. The total drops
    trailing zeros (1M, not 1.00M) since it is a constant per model.
    """
    used = f"{current // 1000}K"
    if size < 1_000_000:
        return used, f"{size // 1000}K"
    return used, f"{f'{size / 1_000_000:.2f}'.rstrip('0').rstrip('.')}M"


def context_segment(data: dict) -> str:
    cw = data.get("context_window")
    usage = cw.get("current_usage") if isinstance(cw, dict) else None
    if not isinstance(usage, dict):
        return f"{GREEN}0%{RESET}"
    current = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        current += int(as_number(usage.get(key)) or 0)
    size = int(as_number(cw.get("context_window_size")) or 0)
    if size <= 0:
        return f"{GREEN}{current // 1000}K{RESET}"
    pct = current * 100 // size
    used, total = format_context(current, size)
    return f"{context_color(pct)}{used}/{total} ({pct}%){RESET}"


# --- Transcript scan (single incremental pass with an offset cache) ---
#
# A session's usage lives in the MAIN transcript plus separate JSONL files for
# subagents/workflows under <transcript-dir>/<session-id>/. All are scanned and
# cached per file; cost is priced per entry from message.model.

def family_tier(model: str) -> tuple:
    """(family label, rates) from the payload display name."""
    for key, label, rates in BASE_TIERS:
        if key in model:
            return label, rates
    return DEFAULT_TIER


def model_tier(model_id, default: tuple, speed=None) -> tuple:
    """(family label, rates) for one transcript entry, keyed on its model id."""
    label, rates = default
    if isinstance(model_id, str):
        mid = model_id.lower()
        for key, tier_label, tier_rates in MODEL_TIERS:
            if key in mid:
                label, rates = tier_label, tier_rates
                break
    if speed == "fast" and rates == (5, 25):
        rates = FAST_RATES  # only Opus 5 / 4.8 can serve fast mode
    return label, rates


def usage_cost(usage: list[int], rates: tuple) -> float:
    # usage: [in, cache_create_total, cache_read, out, cache_create_1h, web_searches]
    # 5-minute cache writes cost 1.25x input rate, 1-hour writes 2x
    input_rate, output_rate = rates
    cc_1h = usage[4]
    cc_5m = usage[1] - cc_1h
    return (
        usage[0] * input_rate
        + cc_5m * input_rate * 1.25
        + cc_1h * input_rate * 2.0
        + usage[2] * input_rate * 0.1
        + usage[3] * output_rate
    ) / 1_000_000 + usage[5] * WEB_SEARCH_USD


def empty_file_state() -> dict:
    return {
        "offset": 0,
        "fp": [0, 0],  # (st_ino, st_dev) — detects file replaced at the same path
        "in": 0,
        "cc": 0,
        "cr": 0,
        "out": 0,
        "usd": 0.0,
        "models": {},  # family label -> usd, so a pricier tier stays visible
        "recent": {},  # message.id -> last usage [in, cc, cr, out], FIFO-bounded
        # Event-walk times (main file only): gaps between consecutive events
        # (real user prompt / assistant entry) longer than IDLE_GAP_S are breaks
        # and count nowhere; shorter ones count as session time, and as waiting
        # time too when they end in an assistant entry after a prompt.
        "wait_s": 0.0,
        "active_s": 0.0,
        # bucket start (epoch, TIME_BUCKET_S aligned) -> [active, wait]
        "buckets": {},
        "buckets_from": 0.0,  # oldest instant the buckets still cover
        "last_event": 0.0,  # timestamp of the newest counted event
        "turn_open": 0.0,  # 1.0 once any prompt was seen (gap→wait attribution)
    }


def empty_session_stats() -> dict:
    return {"v": CACHE_VERSION, "files": {}, "last_task": ""}


def empty_totals() -> dict:
    return {
        "in": 0,
        "cc": 0,
        "cr": 0,
        "out": 0,
        "usd": 0.0,
        "models": {},
        "ai_s": 0,
        "active_s": 0,
        "last_activity": 0.0,
        "last_task": "",
        "buckets": {},
        "buckets_from": 0.0,
        "win_active_s": None,  # same two figures sliced to the current run
        "win_wait_s": None,
    }


def stats_cache_path(transcript_path: str) -> str:
    import hashlib
    import tempfile

    digest = hashlib.md5(transcript_path.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"claude-statusline-{digest}.json")


def is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid_file_state(state) -> dict | None:
    """Strictly validate one cached file state; None on any malformed field.

    Strict per-key validation matters: a cache that passes a partial check but
    breaks later in the scan would never be overwritten, bricking the line.
    """
    if not isinstance(state, dict):
        return None
    out = empty_file_state()
    for key in ("offset",) + USAGE_KEYS:
        if not is_count(state.get(key)):
            return None
        out[key] = state[key]
    for key in ("usd", "wait_s", "active_s", "last_event", "turn_open", "buckets_from"):
        val = state.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
            return None
        out[key] = float(val)
    fp = state.get("fp")
    if isinstance(fp, list) and len(fp) == 2 and all(is_count(v) for v in fp):
        out["fp"] = fp
    buckets = state.get("buckets")
    if isinstance(buckets, dict):
        for start, pair in buckets.items():
            if not isinstance(start, str) or not start.lstrip("-").isdigit():
                continue
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            values = [as_number(v) for v in pair]
            if all(v is not None and v >= 0 for v in values):
                out["buckets"][start] = values
    models = state.get("models")
    if isinstance(models, dict):
        for label, usd in models.items():
            val = as_number(usd)
            if isinstance(label, str) and val is not None and val >= 0:
                out["models"][label] = val
    recent = state.get("recent")
    if isinstance(recent, dict):
        for mid, usage in recent.items():
            if (
                isinstance(mid, str)
                and isinstance(usage, list)
                and len(usage) == 6
                and all(is_count(v) for v in usage)
            ):
                out["recent"][mid] = usage
    return out


def load_session_cache(cache_file: str) -> dict:
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        return empty_session_stats()
    stats = empty_session_stats()
    if not isinstance(cache, dict) or cache.get("v") != CACHE_VERSION:
        return stats
    files = cache.get("files")
    if isinstance(files, dict):
        for path, state in files.items():
            if not isinstance(path, str):
                continue
            valid = valid_file_state(state)
            if valid is not None:
                stats["files"][path] = valid
    if isinstance(cache.get("last_task"), str):
        stats["last_task"] = cache["last_task"]
    return stats


def save_stats_cache(cache_file: str, stats: dict) -> None:
    try:
        tmp = f"{cache_file}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f)
        os.replace(tmp, cache_file)
    except Exception:
        pass


def extract_usage(u: dict) -> list[int]:
    usage = [
        int(as_number(u.get(key)) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    ]
    # 1h cache writes are priced differently; without the breakdown assume 5m.
    # The breakdown sometimes exceeds the legacy total field — trust the larger.
    breakdown = u.get("cache_creation")
    cc_1h = 0
    if isinstance(breakdown, dict):
        cc_5m = int(as_number(breakdown.get("ephemeral_5m_input_tokens")) or 0)
        cc_1h = int(as_number(breakdown.get("ephemeral_1h_input_tokens")) or 0)
        usage[1] = max(usage[1], cc_5m + cc_1h)
        cc_1h = min(cc_1h, usage[1])
    usage.append(cc_1h)
    server_tools = u.get("server_tool_use")
    searches = 0
    if isinstance(server_tools, dict):
        searches = int(as_number(server_tools.get("web_search_requests")) or 0)
    usage.append(searches)
    return usage


def iteration_costs(u: dict, default: tuple, speed=None) -> list:
    """Billed earlier attempts (e.g. model fallback) recorded in usage.iterations.

    The top-level usage equals the LAST iteration; previous ones were separate
    billed calls, often on a different (more expensive) model. Returns
    (family label, usd) pairs so each attempt lands on its own tier.
    """
    iterations = u.get("iterations")
    if not isinstance(iterations, list) or len(iterations) < 2:
        return []
    costs = []
    for attempt in iterations[:-1]:
        if isinstance(attempt, dict):
            label, rates = model_tier(attempt.get("model"), default, speed)
            costs.append((label, usage_cost(extract_usage(attempt), rates)))
    return costs


def accumulate_entry(
    state: dict, line: bytes, entry: dict, default_tier: tuple, session: dict, is_main: bool
) -> None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    u = message.get("usage")
    if isinstance(u, dict):
        usage = extract_usage(u)
        speed = u.get("speed")
        label, rates = model_tier(message.get("model"), default_tier, speed)
        mid = message.get("id")
        recent = state["recent"]
        prev = recent.get(mid) if isinstance(mid, str) else None
        if prev is not None:
            # Same streamed response on another JSONL line: each line repeats
            # (possibly updated) usage, so replace — last line wins. Duplicate
            # lines repeat identical iterations, so those are not re-added.
            for i, key in enumerate(USAGE_KEYS):
                state[key] += usage[i] - prev[i]
            costs = [(label, usage_cost(usage, rates) - usage_cost(prev, rates))]
            recent[mid] = usage
        else:
            for i, key in enumerate(USAGE_KEYS):
                state[key] += usage[i]
            costs = [(label, usage_cost(usage, rates))]
            costs += iteration_costs(u, (label, rates), speed)
            if isinstance(mid, str) and mid:
                if len(recent) >= RECENT_IDS_MAX:
                    recent.pop(next(iter(recent)))
                recent[mid] = usage
        models = state["models"]
        for cost_label, cost_usd in costs:
            state["usd"] += cost_usd
            models[cost_label] = models.get(cost_label, 0.0) + cost_usd
    if is_main and b'"Task"' in line:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Task"
            ):
                block_input = block.get("input")
                desc = block_input.get("description") if isinstance(block_input, dict) else None
                if isinstance(desc, str) and desc:
                    session["last_task"] = desc


def add_time(state: dict, ts: float, gap: float, waited: bool) -> None:
    """Book one counted gap into the running totals and its 15-minute bucket.

    The gap is filed under the bucket it ends in — gaps are capped at
    IDLE_GAP_S, so the misattribution only ever shows at a window edge.
    """
    state["active_s"] += gap
    if waited:
        state["wait_s"] += gap
    key = str(int(ts // TIME_BUCKET_S) * TIME_BUCKET_S)
    bucket = state["buckets"].get(key)
    if bucket is None:
        state["buckets"][key] = [gap, gap if waited else 0.0]
    else:
        bucket[0] += gap
        if waited:
            bucket[1] += gap


def prune_buckets(state: dict) -> None:
    cutoff = time.time() - TIME_BUCKET_KEEP_S
    stale = [k for k in state["buckets"] if int(k) + TIME_BUCKET_S <= cutoff]
    for key in stale:
        del state["buckets"][key]
    if stale:
        state["buckets_from"] = max(state["buckets_from"], cutoff)


def window_times(state: dict, since: float) -> tuple | None:
    """(active, wait) inside [since, now]; None when the buckets miss the start.

    A run older than the bucket retention cannot be sliced, and reporting a
    truncated figure as the session length would be worse than falling back.
    """
    if since < state["buckets_from"]:
        return None
    active = wait = 0.0
    for key, (bucket_active, bucket_wait) in state["buckets"].items():
        start = int(key)
        end = start + TIME_BUCKET_S
        if end <= since:
            continue
        # The bucket the window opens in is counted pro rata, otherwise work
        # done just before the resume would be credited to the current run.
        share = 1.0 if start >= since else (end - since) / TIME_BUCKET_S
        active += bucket_active * share
        wait += bucket_wait * share
    return active, wait


def scan_file(
    path: str, state: dict, default_tier: tuple, session: dict, is_main: bool
) -> bool:
    """Incrementally scan one append-only JSONL file; True if state changed."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    fp = [int(getattr(st, "st_ino", 0) or 0), int(getattr(st, "st_dev", 0) or 0)]
    changed = False
    if state["fp"] != fp or st.st_size < state["offset"]:
        # New inode at the same path or truncation — cached offsets are invalid
        fresh = empty_file_state()
        fresh["fp"] = fp
        state.clear()
        state.update(fresh)
        changed = True
    if st.st_size == state["offset"]:
        return changed
    try:
        with open(path, "rb") as f:
            f.seek(state["offset"])
            offset = state["offset"]
            for line in f:
                if not line.endswith(b"\n"):
                    break  # partial trailing write — picked up on the next render
                offset += len(line)
                # Cheap byte prefilters; tool results always carry "toolUseResult"
                user_candidate = (
                    is_main
                    and (b'"type":"user"' in line or b'"type": "user"' in line)
                    and b'"toolUseResult"' not in line
                )
                if not user_candidate and b'"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                etype = entry.get("type")
                if etype == "user" and user_candidate and not entry.get("isMeta"):
                    # Real user prompt: gap since the last event is the user's
                    # think time — session time when short, a break when long
                    ts = parse_ts(entry.get("timestamp"))
                    if ts > 0:
                        gap = ts - state["last_event"]
                        if state["last_event"] > 0 and 0 < gap <= IDLE_GAP_S:
                            add_time(state, ts, gap, waited=False)
                        state["last_event"] = max(state["last_event"], ts)
                        state["turn_open"] = 1.0
                elif etype == "assistant":
                    if is_main:
                        # Gap ending in an assistant entry is AI working time;
                        # over the threshold it's a stall/resume break instead
                        ts = parse_ts(entry.get("timestamp"))
                        if ts > 0:
                            gap = ts - state["last_event"]
                            if state["last_event"] > 0 and 0 < gap <= IDLE_GAP_S:
                                add_time(state, ts, gap, waited=state["turn_open"] > 0)
                            state["last_event"] = max(state["last_event"], ts)
                    try:
                        accumulate_entry(state, line, entry, default_tier, session, is_main)
                    except Exception:
                        continue  # one poison line must not stall the offset cache
        if offset != state["offset"]:
            state["offset"] = offset
            changed = True
    except OSError:
        pass
    return changed


def subagent_files(transcript_path: str) -> list[str]:
    """JSONL transcripts of subagents/workflows: <transcript-dir>/<session-id>/**."""
    session_dir = os.path.splitext(transcript_path)[0]
    if not os.path.isdir(session_dir):
        return []
    found = []
    try:
        for root, _dirs, names in os.walk(session_dir):
            for name in names:
                if name.endswith(".jsonl"):
                    found.append(os.path.join(root, name))
    except OSError:
        pass
    return sorted(found)


def scan_session(transcript_path: str, default_tier: tuple) -> dict:
    """Aggregate usage and cost over the main transcript and all subagent files.

    States for files that vanished are kept — their cost was incurred. Only the
    main transcript feeds the task label.
    """
    cache_file = stats_cache_path(transcript_path)
    session = load_session_cache(cache_file)
    changed = False
    for path in [transcript_path] + subagent_files(transcript_path):
        state = session["files"].get(path)
        if state is None:
            state = empty_file_state()
            session["files"][path] = state
        changed |= scan_file(
            path, state, default_tier, session, is_main=(path == transcript_path)
        )
    main_state = session["files"].get(transcript_path)
    if main_state is not None:
        before = len(main_state["buckets"])
        prune_buckets(main_state)
        changed |= len(main_state["buckets"]) != before
    if changed:
        save_stats_cache(cache_file, session)
    totals = empty_totals()
    for state in session["files"].values():
        for key in USAGE_KEYS:
            totals[key] += state[key]
        totals["usd"] += state["usd"]
        for label, usd in state["models"].items():
            totals["models"][label] = totals["models"].get(label, 0.0) + usd
    if main_state is not None:
        totals["ai_s"] = int(main_state["wait_s"])
        totals["active_s"] = int(main_state["active_s"])
        totals["last_activity"] = main_state["last_event"]
        totals["buckets"] = main_state["buckets"]
        totals["buckets_from"] = main_state["buckets_from"]
    totals["last_task"] = session["last_task"]
    return totals


# --- Cost ---

def format_money(value: float) -> str:
    # Above $9.99 cents are noise — show whole dollars
    return f"${value:.0f}" if value > 9.99 else f"${value:.2f}"


def cost_segment(data: dict, stats: dict) -> str:
    """Official session cost plus burn rate; the own estimate only when opted in.

    "n/a" holds the slot until the first API response puts cost in the payload —
    a segment that vanishes mid-session shifts every field after it.
    """
    cost_obj = data.get("cost")
    official = duration_ms = None
    if isinstance(cost_obj, dict):
        official = as_number(cost_obj.get("total_cost_usd"))
        duration_ms = as_number(cost_obj.get("total_duration_ms"))
    estimate = stats["usd"] if estimates_enabled() else None

    burn = ""
    burn_base = official if official is not None else (estimate if estimate else None)
    # $ per hour of ACTIVE session time (breaks >1h excluded), so a long idle
    # gap doesn't dilute the rate. Each cost is divided by time from its OWN
    # scope: the official figure covers this run, the estimate the whole
    # transcript. Payload wall duration is the last-resort fallback.
    if official is not None and stats["win_active_s"] is not None:
        active = active_session_time(stats, stats["win_active_s"])
    else:
        active = active_session_time(stats)
    if active is None and duration_ms and duration_ms > 0:
        active = duration_ms / 1000
    if burn_base is not None and active and active >= BURN_RATE_MIN_S:
        burn = f"{format_money(burn_base / (active / 3600))}/h"
    tail = f" {burn}" if burn else ""

    if estimate is None:
        if official is None:
            return "n/a"
        return f"{format_money(official)} ({burn})" if burn else format_money(official)
    if official is not None:
        return f"~${estimate:.2f} ({format_money(official)}{tail})"
    return f"~${estimate:.2f}{tail}"


def tokens_segment(stats: dict) -> str:
    total_all_input = stats["in"] + stats["cc"] + stats["cr"]
    cache_info = ""
    if total_all_input > 0 and stats["cr"] > 0:
        cache_info = f" C:{stats['cr'] * 100 // total_all_input}%"
    return f"↓{format_tokens(total_all_input)} ↑{format_tokens(stats['out'])}{cache_info}"


def lines_segment(data: dict) -> str:
    cost_obj = data.get("cost")
    if not isinstance(cost_obj, dict):
        return ""
    added = int(as_number(cost_obj.get("total_lines_added")) or 0)
    removed = int(as_number(cost_obj.get("total_lines_removed")) or 0)
    if added <= 0 and removed <= 0:
        return ""
    return f"{GREEN}+{added}{RESET}/{RED}-{removed}{RESET}"


# --- Model-scoped usage windows (plan usage endpoint) ---

def usage_cache_path() -> str:
    import tempfile

    # Shared by every session on the machine: five open terminals still make
    # one request per TTL, not five.
    return os.path.join(tempfile.gettempdir(), "claude-statusline-usage.json")


def valid_usage_cache(cache) -> dict | None:
    if not isinstance(cache, dict) or cache.get("v") != USAGE_CACHE_VERSION:
        return None
    out = {"v": USAGE_CACHE_VERSION, "windows": []}
    for key in ("ts", "ok_ts", "next_try"):
        val = as_number(cache.get(key))
        if val is None or val < 0:
            return None
        out[key] = val
    windows = cache.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not isinstance(window, dict):
            continue
        label = window.get("label")
        pct = as_number(window.get("pct"))
        resets = as_number(window.get("resets_at"))
        if isinstance(label, str) and label and pct is not None:
            out["windows"].append({"label": label, "pct": pct, "resets_at": resets or 0.0})
    return out


def read_usage_cache() -> dict | None:
    try:
        with open(usage_cache_path(), "r", encoding="utf-8") as f:
            return valid_usage_cache(json.load(f))
    except Exception:
        return None


def write_usage_cache(windows: list, ok_ts: float, retry_s: float) -> None:
    now = time.time()
    cache = {
        "v": USAGE_CACHE_VERSION,
        "ts": now,
        "ok_ts": ok_ts,
        "next_try": now + retry_s,
        "windows": windows,
    }
    path = usage_cache_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        pass


def oauth_access_token() -> str | None:
    """Claude Code's own OAuth token — credentials file, else the macOS Keychain.

    Never persisted anywhere by this script; only the resulting percentages are
    cached. An expired token is treated as absent: Claude Code owns the refresh
    and racing it would burn the refresh token.
    """
    raw = ""
    try:
        with open(
            os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json"),
            "r", encoding="utf-8",
        ) as f:
            raw = f.read()
    except OSError:
        if sys.platform != "darwin":
            return None
        import subprocess

        try:
            raw = subprocess.check_output(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                stderr=subprocess.DEVNULL, text=True, timeout=10,
            )
        except Exception:
            return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth")
    except Exception:
        return None
    if not isinstance(oauth, dict):
        return None
    expires = as_number(oauth.get("expiresAt"))
    if expires is not None and expires / 1000 <= time.time():
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def scoped_windows(body: dict) -> list:
    """weekly_scoped entries of limits[] — one per model bucket (e.g. Fable).

    Keyed off `kind` rather than fixed field names: seven_day_opus and
    seven_day_sonnet are null on plans that only have a Fable bucket, and the
    set of scoped models changes server-side.
    """
    found = []
    limits = body.get("limits")
    if not isinstance(limits, list):
        return found
    for item in limits:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        label = model.get("display_name") if isinstance(model, dict) else None
        pct = as_number(item.get("percent"))
        if not isinstance(label, str) or not label or pct is None:
            continue
        found.append({
            "label": label[:12],
            "pct": pct,
            "resets_at": parse_ts(item.get("resets_at")),
        })
    return found


def short_label(label: str, initials: list) -> str:
    """"Fable" -> "F"; the full label only when two buckets share an initial."""
    initial = label[:1].upper()
    return initial if initials.count(initial) == 1 else label


def refresh_usage_cache() -> None:
    """`--refresh-usage` entry point: one GET, then rewrite the shared cache."""
    lock = f"{usage_cache_path()}.lock"
    try:
        if time.time() - os.stat(lock).st_mtime < USAGE_LOCK_S:
            return  # another refresher is in flight
        os.unlink(lock)
    except OSError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return
    try:
        previous = read_usage_cache()
        token = oauth_access_token()
        if token is None:
            body, status = None, 401
        else:
            body, status = usage_request(token)
        if status == 200 and isinstance(body, dict):
            write_usage_cache(scoped_windows(body), time.time(), 0)
        else:
            # Keep the last good numbers; the render side ages them out.
            retry = USAGE_RETRY_AUTH_S if status in (401, 403) else USAGE_RETRY_NET_S
            write_usage_cache(
                previous["windows"] if previous else [],
                previous["ok_ts"] if previous else 0.0,
                retry,
            )
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def usage_request(token: str) -> tuple:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-statusline",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8", "replace")), response.status
    except urllib.error.HTTPError as err:
        return None, err.code
    except Exception:
        return None, 0


def spawn_usage_refresh() -> None:
    """Fire and forget — the render must never wait on the network."""
    import subprocess

    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh-usage"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        pass


def scoped_limit_parts() -> list:
    if os.environ.get("STATUSLINE_NO_USAGE_API") == "1":
        return []
    cache = read_usage_cache()
    now = time.time()
    if cache is None:
        spawn_usage_refresh()
        return []
    if now - cache["ts"] >= USAGE_CACHE_TTL_S and now >= cache["next_try"]:
        spawn_usage_refresh()  # this render still draws the cached numbers
    if now - cache["ok_ts"] > USAGE_STALE_S:
        return []
    live = [w for w in cache["windows"] if not w["resets_at"] or w["resets_at"] > now]
    initials = [w["label"][:1].upper() for w in live]
    return [
        f"{rate_color(w['pct'])}{short_label(w['label'], initials)}:{w['pct']:.0f}%{RESET}"
        for w in live
    ]


# --- Rate limits ---

def rate_color(pct: float) -> str:
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GRAY


def format_remaining(resets_at) -> str:
    resets = as_number(resets_at)
    if not resets:
        return ""
    remaining = int(resets - time.time())
    if remaining <= 0:
        return ""
    if remaining < 3600:
        return f"{remaining // 60}m"
    if remaining < 172_800:  # up to 47h stays in hours — "1d" for 11h misleads
        return f"{math.ceil(remaining / 3600)}h"
    return f"{round(remaining / 86400)}d"


def rate_limits_segment(data: dict) -> str:
    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        return ""
    parts = []
    for key, fallback in (("five_hour", "5h"), ("seven_day", "7d")):
        window = rl.get(key)
        if not isinstance(window, dict):
            continue
        pct = as_number(window.get("used_percentage"))
        if pct is None:
            continue
        label = format_remaining(window.get("resets_at")) or fallback
        parts.append(f"{rate_color(pct)}{label}:{pct:.0f}%{RESET}")
    parts.extend(scoped_limit_parts())  # per-model weekly buckets (e.g. Fable)
    return " ".join(parts)


# --- Session duration ---

def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"


def apply_process_window(data: dict, stats: dict) -> None:
    """Slice active and wait time to the run the payload's cost belongs to.

    total_cost_usd and total_duration_ms both restart on resume while the
    transcript keeps growing, so a two-month transcript would otherwise pair one
    run's cost with two months of clock.
    """
    cost_obj = data.get("cost")
    ms = as_number(cost_obj.get("total_duration_ms")) if isinstance(cost_obj, dict) else None
    if not ms or ms <= 0:
        return
    times = window_times(stats, time.time() - ms / 1000)
    if times is not None:
        stats["win_active_s"], stats["win_wait_s"] = times


def format_span(seconds: float) -> str:
    """Two digits of transcript total: hours up to 99h, days beyond."""
    hours = math.ceil(seconds / 3600)
    return f"{hours}h" if hours < 100 else f"{round(seconds / 86400)}d"


def transcript_span(stats: dict, shown: int) -> str:
    """Whole-transcript session time, but only when it adds something.

    A fresh session has nothing before the current run, so the two figures
    coincide and the suffix would be noise; it is equally pointless when the
    block is already configured to show the transcript total.
    """
    if os.environ.get("STATUSLINE_DURATION_TOTAL") == "1" or stats["win_active_s"] is None:
        return ""
    total = active_session_time(stats)
    if total is None or total <= shown + SPAN_MIN_DELTA_S:
        return ""
    return f" / {format_span(total)}"


def duration_scope(stats: dict) -> tuple:
    """(active, wait) for the configured scope — current run unless asked."""
    if os.environ.get("STATUSLINE_DURATION_TOTAL") == "1" or stats["win_active_s"] is None:
        return stats["active_s"], stats["ai_s"]
    return stats["win_active_s"], stats["win_wait_s"]


def active_session_time(stats: dict, active: float | None = None) -> int | None:
    """Session time minus idle breaks over IDLE_GAP_S, plus the live tail.

    Counts turn spans plus inter-turn gaps up to the threshold; the time since
    the last activity ticks live and stops counting once it exceeds the gap.
    Returns None when the transcript yielded no turn data.
    """
    if stats["last_activity"] <= 0:
        return None
    if active is None:
        active = stats["active_s"]
    tail = time.time() - stats["last_activity"]
    if 0 < tail <= IDLE_GAP_S:
        active += int(tail)
    return int(active)


def duration_segment(data: dict, transcript_path: str, stats: dict) -> str:
    active, wait = duration_scope(stats)
    seconds = active_session_time(stats, active)
    if seconds is None:
        # Fallbacks when the transcript has no turn data: payload wall duration
        # (survives resumed sessions), then transcript file birthtime
        cost_obj = data.get("cost")
        ms = as_number(cost_obj.get("total_duration_ms")) if isinstance(cost_obj, dict) else None
        if ms and ms > 0:
            seconds = int(ms // 1000)
        elif transcript_path and os.path.isfile(transcript_path):
            start_time = get_file_creation_time(transcript_path)
            seconds = int(time.time() - start_time) if start_time > 0 else 0
        else:
            seconds = 0
    elapsed = format_duration(seconds)
    if wait > 0:
        # In parentheses: wall-clock the user actually waited for responses
        # (sum of user-prompt → last-assistant-entry spans from the transcript;
        # NOT total_api_duration_ms, which multiply-counts parallel subagents).
        # Same scope as the figure before it, so it is always contained in it.
        elapsed += f" ({WAIT_MARK}{format_duration(int(wait))})"
    # Trailing "/ 42h": how long the whole transcript has been worked on, idle
    # breaks excluded the same way, when the run is only part of it.
    return elapsed + transcript_span(stats, seconds)


# --- Git ---

def find_git_root(cwd: str) -> str | None:
    """Nearest ancestor containing .git — a dir (repo) or a file (worktree/submodule)."""
    path = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def parse_branch_header(header: str) -> str:
    # "main...origin/main [ahead 1]" | "main" | "HEAD (no branch)" | "No commits yet on main"
    if header.startswith("No commits yet on "):
        return header[len("No commits yet on "):]
    if header.startswith("HEAD ("):  # detached; a branch named "HEAD-x" must NOT match
        return "HEAD"
    return header.split("...", 1)[0]


def git_segment(cwd: str) -> str:
    if find_git_root(cwd) is None:
        return ""
    import subprocess

    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        out = subprocess.check_output(
            ["git", "-c", "core.fileMode=false", "status", "--porcelain", "-b"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=1.5, env=env,
        )
    except Exception:
        return ""  # not a repo, git missing, or git timed out
    lines = out.splitlines()
    if not lines or not lines[0].startswith("## "):
        return ""
    branch = parse_branch_header(lines[0][3:])

    added = modified = deleted = 0
    for status_line in lines[1:]:
        if len(status_line) < 2:
            continue
        x, y = status_line[0], status_line[1]
        if status_line.startswith("??") or x == "A":
            added += 1
        elif x == "D" or y == "D":
            deleted += 1
        elif x in "MRC" or y in "MRC":
            modified += 1
    if added or modified or deleted:
        dirty = " ●"
        if added:
            dirty += f" +{added}"
        if modified:
            dirty += f" ~{modified}"
        if deleted:
            dirty += f" -{deleted}"
    else:
        dirty = " ○"
    return f"{CYAN}{branch}{dirty}{RESET}{workspace_diff(cwd, env)}"


def workspace_diff(cwd: str, env: dict) -> str:
    """Uncommitted line delta vs HEAD (staged + unstaged) — zeroes after commit."""
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "-c", "core.fileMode=false", "diff", "--numstat", "HEAD"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=1.5, env=env,
        )
    except Exception:
        return ""  # e.g. repo without commits yet, or git timed out
    added = removed = 0
    for diff_line in out.splitlines():
        parts = diff_line.split("\t")
        if len(parts) >= 2:  # binary files report "-" instead of counts
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                removed += int(parts[1])
    if added == 0 and removed == 0:
        return ""
    return f" ({GREEN}+{added}{RESET}/{RED}-{removed}{RESET})"


# --- Task label ---

def task_segment(task_name: str) -> str:
    if not task_name:
        return ""
    if len(task_name) > 45:
        task_name = task_name[:42] + "..."
    return task_name


# --- Assembly ---

def elapsed_ms() -> float:
    return (time.perf_counter() - START_TIME) * 1000


def render_segment() -> str:
    return f"{GRAY}{elapsed_ms():.0f}ms{RESET}"


def render_time_segment() -> str:
    """Render cost plus the wall clock, in the machine's local time."""
    return f"{GRAY}{elapsed_ms():.0f}ms ({time.strftime('%H:%M')}){RESET}"


# Default order, applied when the command carries no block argument.
BLOCK_ORDER = (
    "context", "duration", "cost", "model", "tokens",
    "lines", "limits", "git", "task", "render",
)
# Blocks outside the default line — available only by naming them explicitly.
EXTRA_BLOCKS = ("render+time",)
BLOCK_NAMES = BLOCK_ORDER + EXTRA_BLOCKS
# "branch - task" reads as one unit, so the task block keeps the dash it had.
BLOCK_JOINERS = {"task": " - "}
BLOCK_SEPARATOR = " | "


def parse_blocks(argv: list) -> tuple:
    """Block order from the command argument, e.g. "context,cost,model git".

    Comma- or space-separated, case-insensitive; unknown names are dropped so a
    single typo cannot blank the line, and an argument with nothing usable in it
    falls back to the default order. Names outside the default order (see
    EXTRA_BLOCKS) are valid too — they simply have to be asked for.
    """
    raw = ",".join(arg for arg in argv if not arg.startswith("-"))
    if not raw.strip():
        return BLOCK_ORDER
    order = []
    for name in raw.replace(",", " ").split():
        name = name.lower()
        if name in BLOCK_NAMES and name not in order:
            order.append(name)  # deduplicated: rendering git twice costs two forks
    return tuple(order) or BLOCK_ORDER


def build_line(data: dict, order: tuple = BLOCK_ORDER) -> str:
    model = model_segment(data)
    workspace = data.get("workspace")
    cwd = workspace.get("current_dir") if isinstance(workspace, dict) else None
    if not isinstance(cwd, str):
        cwd = ""  # no workspace in payload → no git segment (don't leak process CWD)
    transcript_path = data.get("transcript_path")
    if not isinstance(transcript_path, str):
        transcript_path = ""

    tier = family_tier(model)
    if transcript_path and os.path.isfile(transcript_path):
        stats = scan_session(transcript_path, tier)
    else:
        stats = empty_totals()
    apply_process_window(data, stats)

    # Lazy on purpose: a block left out of the order is never computed, so
    # dropping "git" also drops its two subprocess calls.
    blocks = {
        "context": lambda: context_segment(data),
        "duration": lambda: f"{MAGENTA}{duration_segment(data, transcript_path, stats)}{RESET}",
        "cost": lambda: f"{YELLOW}{cost_segment(data, stats)}{RESET}",
        "model": lambda: (
            f"{BLUE}{model}{RESET}{GRAY}{model_mix_segment(tier[0], stats)}{RESET}"
        ),
        "tokens": lambda: f"{CYAN}{tokens_segment(stats)}{RESET}",
        "lines": lambda: lines_segment(data),
        "limits": lambda: rate_limits_segment(data),
        "git": lambda: git_segment(cwd) if cwd else "",
        "task": lambda: task_segment(stats["last_task"]),
        "render": render_segment,
        "render+time": render_time_segment,
    }

    line = ""
    for name in order:
        make = blocks.get(name)
        if make is None:
            continue
        text = make()
        if not text:
            continue
        line += (BLOCK_JOINERS.get(name, BLOCK_SEPARATOR) if line else "") + text
    return line


def safe_print(line: str) -> None:
    try:
        print(line, end="")
        sys.stdout.flush()
    except OSError:  # consumer closed the pipe — avoid "Exception ignored" noise
        os._exit(0)


def main() -> None:
    if "--refresh-usage" in sys.argv[1:]:
        refresh_usage_cache()
        return
    try:
        # Bytes + lenient decode: invalid UTF-8 on stdin must not raise
        raw = sys.stdin.buffer.read().decode("utf-8", "replace") if sys.stdin else ""
    except Exception:
        raw = ""
    try:
        data = json.loads(raw)
    except Exception:  # includes RecursionError on hostile nesting
        data = None
    if not isinstance(data, dict):
        safe_print("")
        return

    # DEBUG: dump raw input when STATUSLINE_DEBUG=1
    if os.environ.get("STATUSLINE_DEBUG") == "1":
        try:
            debug_path = os.path.join(os.path.dirname(__file__), "debug_input.json")
            with open(debug_path, "w", encoding="utf-8") as df:
                json.dump(data, df, indent=2)
        except OSError:
            pass

    try:
        line = build_line(data, parse_blocks(sys.argv[1:]))
    except Exception:
        # Last-resort guard: a broken payload must not break the statusline
        try:
            line = model_segment(data)
        except Exception:
            line = ""
        if line:
            line += f"{BLOCK_SEPARATOR}{render_segment()}"
    safe_print(line)


if __name__ == "__main__":
    # Ensure stdout can handle Unicode on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
