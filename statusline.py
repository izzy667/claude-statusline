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

# Rates per MTok: (input, output); cache: write=1.25x input, read=0.1x input
# Verified against platform.claude.com pricing, 2026-06.
# Display-name families — fallback for transcript entries without a model id.
# More specific keys first — "Opus 4.1" must win over "Opus".
BASE_RATES = {
    "Opus 4.1": (15, 75),  # deprecated, retires 2026-08-05
    "Fable": (10, 50),
    "Mythos": (10, 50),
    "Opus": (5, 25),
    "Sonnet": (3, 15),
    "Haiku": (1, 5),
}
# Model-id substrings (lowercase, most specific first) — per-entry pricing;
# one session mixes models (subagents often run on a different tier).
MODEL_RATES = (
    ("opus-4-1", (15, 75)),  # deprecated, retires 2026-08-05
    ("fable", (10, 50)),
    ("mythos", (10, 50)),
    ("opus", (5, 25)),
    ("haiku", (1, 5)),
    ("sonnet", (3, 15)),
)
DEFAULT_RATE = (3, 15)

EFFORT_ABBREV = {"low": "low", "medium": "med", "high": "high", "max": "max", "auto": "auto"}

# Bump on schema OR pricing changes — cached usd values depend on the rate tables
CACHE_VERSION = 4
USAGE_KEYS = ("in", "cc", "cr", "out")
BURN_RATE_MIN_MS = 300_000  # hide burn rate for sessions under 5 minutes (too noisy)
RECENT_IDS_MAX = 16  # dedup window: streamed duplicates are near-consecutive in practice


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
    # Compact context size hints: "(1M context)" → "1M"
    for tag in ("(1M context)", "(200K context)"):
        if tag in model:
            model = model.replace(tag, tag[1:tag.index(" ")])
            break
    effort = get_effort(data)
    if effort:
        model = f"{model} E/{effort}"
    return model


# --- Context window ---

def context_color(pct: float) -> str:
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


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
    return f"{context_color(pct)}{current // 1000}K/{size // 1000}K ({pct}%){RESET}"


# --- Transcript scan (single incremental pass with an offset cache) ---
#
# A session's usage lives in the MAIN transcript plus separate JSONL files for
# subagents/workflows under <transcript-dir>/<session-id>/. All are scanned and
# cached per file; cost is priced per entry from message.model.

def family_rates(model: str) -> tuple:
    for key, rates in BASE_RATES.items():
        if key in model:
            return rates
    return DEFAULT_RATE


def model_rates(model_id, default: tuple) -> tuple:
    if isinstance(model_id, str):
        mid = model_id.lower()
        for key, rates in MODEL_RATES:
            if key in mid:
                return rates
    return default


def usage_cost(usage: list[int], rates: tuple) -> float:
    input_rate, output_rate = rates
    return (
        usage[0] * input_rate
        + usage[1] * input_rate * 1.25
        + usage[2] * input_rate * 0.1
        + usage[3] * output_rate
    ) / 1_000_000


def empty_file_state() -> dict:
    return {
        "offset": 0,
        "fp": [0, 0],  # (st_ino, st_dev) — detects file replaced at the same path
        "in": 0,
        "cc": 0,
        "cr": 0,
        "out": 0,
        "usd": 0.0,
        "recent": {},  # message.id -> last usage [in, cc, cr, out], FIFO-bounded
        # AI working-time (wall-clock the user actually waited), main file only:
        "wait_s": 0.0,  # closed turns: sum of (last assistant ts - user prompt ts)
        "turn_start": 0.0,  # timestamp of the user prompt opening the current turn
        "last_ai": 0.0,  # timestamp of the newest assistant entry in that turn
    }


def empty_session_stats() -> dict:
    return {"v": CACHE_VERSION, "files": {}, "last_task": ""}


def empty_totals() -> dict:
    return {"in": 0, "cc": 0, "cr": 0, "out": 0, "usd": 0.0, "ai_s": 0, "last_task": ""}


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
    for key in ("usd", "wait_s", "turn_start", "last_ai"):
        val = state.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
            return None
        out[key] = float(val)
    fp = state.get("fp")
    if isinstance(fp, list) and len(fp) == 2 and all(is_count(v) for v in fp):
        out["fp"] = fp
    recent = state.get("recent")
    if isinstance(recent, dict):
        for mid, usage in recent.items():
            if (
                isinstance(mid, str)
                and isinstance(usage, list)
                and len(usage) == 4
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


def extract_usage(message: dict) -> list[int] | None:
    u = message.get("usage")
    if not isinstance(u, dict):
        return None
    return [
        int(as_number(u.get(key)) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    ]


def accumulate_entry(
    state: dict, line: bytes, entry: dict, default_rates: tuple, session: dict, is_main: bool
) -> None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    usage = extract_usage(message)
    if usage is not None:
        rates = model_rates(message.get("model"), default_rates)
        mid = message.get("id")
        recent = state["recent"]
        prev = recent.get(mid) if isinstance(mid, str) else None
        if prev is not None:
            # Same streamed response on another JSONL line: each line repeats
            # (possibly updated) usage, so replace — last line wins.
            for i, key in enumerate(USAGE_KEYS):
                state[key] += usage[i] - prev[i]
            state["usd"] += usage_cost(usage, rates) - usage_cost(prev, rates)
            recent[mid] = usage
        else:
            for i, key in enumerate(USAGE_KEYS):
                state[key] += usage[i]
            state["usd"] += usage_cost(usage, rates)
            if isinstance(mid, str) and mid:
                if len(recent) >= RECENT_IDS_MAX:
                    recent.pop(next(iter(recent)))
                recent[mid] = usage
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


def scan_file(
    path: str, state: dict, default_rates: tuple, session: dict, is_main: bool
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
                    # Real user prompt: close the previous AI turn, open a new one
                    ts = parse_ts(entry.get("timestamp"))
                    if ts > 0:
                        if state["turn_start"] > 0 and state["last_ai"] > state["turn_start"]:
                            state["wait_s"] += state["last_ai"] - state["turn_start"]
                        state["turn_start"] = ts
                        state["last_ai"] = 0.0
                elif etype == "assistant":
                    if is_main and state["turn_start"] > 0:
                        ts = parse_ts(entry.get("timestamp"))
                        if ts > state["last_ai"]:
                            state["last_ai"] = ts
                    try:
                        accumulate_entry(state, line, entry, default_rates, session, is_main)
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


def scan_session(transcript_path: str, default_rates: tuple) -> dict:
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
            path, state, default_rates, session, is_main=(path == transcript_path)
        )
    if changed:
        save_stats_cache(cache_file, session)
    totals = empty_totals()
    for state in session["files"].values():
        for key in USAGE_KEYS:
            totals[key] += state[key]
        totals["usd"] += state["usd"]
    main_state = session["files"].get(transcript_path)
    if main_state is not None:
        wait = main_state["wait_s"]
        if main_state["turn_start"] > 0 and main_state["last_ai"] > main_state["turn_start"]:
            wait += main_state["last_ai"] - main_state["turn_start"]  # turn in progress
        totals["ai_s"] = int(wait)
    totals["last_task"] = session["last_task"]
    return totals


# --- Cost ---

def format_money(value: float) -> str:
    # Above $9.99 cents are noise — show whole dollars
    return f"${value:.0f}" if value > 9.99 else f"${value:.2f}"


def cost_segment(data: dict, stats: dict) -> str:
    cost = stats["usd"]
    cost_obj = data.get("cost")
    official = duration_ms = None
    if isinstance(cost_obj, dict):
        official = as_number(cost_obj.get("total_cost_usd"))
        duration_ms = as_number(cost_obj.get("total_duration_ms"))

    burn = ""
    burn_base = official if official is not None else (cost if cost > 0 else None)
    if burn_base is not None and duration_ms and duration_ms >= BURN_RATE_MIN_MS:
        burn = f" {format_money(burn_base / (duration_ms / 3_600_000))}/h"

    if official is not None:
        return f"~${cost:.2f} ({format_money(official)}{burn})"
    return f"~${cost:.2f}{burn}"


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
    return f" | {GREEN}+{added}{RESET}/{RED}-{removed}{RESET}"


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
    return f" | {' '.join(parts)}" if parts else ""


# --- Session duration ---

def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"


def duration_segment(data: dict, transcript_path: str, stats: dict) -> str:
    # Payload duration survives resumed sessions; file birthtime is the fallback
    cost_obj = data.get("cost")
    ms = as_number(cost_obj.get("total_duration_ms")) if isinstance(cost_obj, dict) else None
    if ms and ms > 0:
        elapsed = format_duration(int(ms // 1000))
    elif transcript_path and os.path.isfile(transcript_path):
        start_time = get_file_creation_time(transcript_path)
        elapsed = format_duration(int(time.time() - start_time)) if start_time > 0 else "0m"
    else:
        elapsed = "0m"
    if stats["ai_s"] > 0:
        # In parentheses: wall-clock the user actually waited for responses
        # (sum of user-prompt → last-assistant-entry spans from the transcript;
        # NOT total_api_duration_ms, which multiply-counts parallel subagents)
        elapsed += f" ({format_duration(stats['ai_s'])})"
    return elapsed


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
    return f" | {CYAN}{branch}{dirty}{RESET}"


# --- Task label ---

def task_segment(task_name: str) -> str:
    if not task_name:
        return ""
    if len(task_name) > 45:
        task_name = task_name[:42] + "..."
    return f" - {task_name}"


# --- Assembly ---

def build_line(data: dict) -> str:
    model = model_segment(data)
    workspace = data.get("workspace")
    cwd = workspace.get("current_dir") if isinstance(workspace, dict) else None
    if not isinstance(cwd, str):
        cwd = ""  # no workspace in payload → no git segment (don't leak process CWD)
    transcript_path = data.get("transcript_path")
    if not isinstance(transcript_path, str):
        transcript_path = ""

    if transcript_path and os.path.isfile(transcript_path):
        stats = scan_session(transcript_path, family_rates(model))
    else:
        stats = empty_totals()

    return (
        f"{context_segment(data)}"
        f" | {MAGENTA}{duration_segment(data, transcript_path, stats)}{RESET}"
        f" | {YELLOW}{cost_segment(data, stats)}{RESET}"
        f" | {BLUE}{model}{RESET}"
        f" | {CYAN}{tokens_segment(stats)}{RESET}"
        f"{lines_segment(data)}"
        f"{rate_limits_segment(data)}"
        f"{git_segment(cwd) if cwd else ''}"
        f"{task_segment(stats['last_task'])}"
    )


def safe_print(line: str) -> None:
    try:
        print(line, end="")
        sys.stdout.flush()
    except OSError:  # consumer closed the pipe — avoid "Exception ignored" noise
        os._exit(0)


def main() -> None:
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
        line = build_line(data)
    except Exception:
        # Last-resort guard: a broken payload must not break the statusline
        try:
            line = model_segment(data)
        except Exception:
            line = ""
    if line:
        elapsed_ms = (time.perf_counter() - START_TIME) * 1000
        line += f" | {GRAY}{elapsed_ms:.0f}ms{RESET}"
    safe_print(line)


if __name__ == "__main__":
    # Ensure stdout can handle Unicode on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
