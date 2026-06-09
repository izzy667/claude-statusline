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

RESET = "\033[0m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BLUE = "\033[0;34m"
MAGENTA = "\033[0;35m"
CYAN = "\033[0;36m"
GRAY = "\033[0;37m"

# Base rates per MTok: (input, output); cache: write=1.25x input, read=0.1x input
# Verified against platform.claude.com pricing, 2026-06.
# More specific keys first — "Opus 4.1" must win over "Opus".
BASE_RATES = {
    "Opus 4.1": (15, 75),  # deprecated, retires 2026-08-05
    "Fable": (10, 50),
    "Mythos": (10, 50),
    "Opus": (5, 25),
    "Sonnet": (3, 15),
    "Haiku": (1, 5),
}
DEFAULT_RATE = (3, 15)

EFFORT_ABBREV = {"low": "low", "medium": "med", "high": "high", "max": "max", "auto": "auto"}

CACHE_VERSION = 2
USAGE_KEYS = ("in", "cc", "cr", "out")
BURN_RATE_MIN_MS = 300_000  # hide burn rate for sessions under 5 minutes (too noisy)
RECENT_IDS_MAX = 50  # dedup window: streamed duplicates are near-consecutive in practice


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

def empty_stats() -> dict:
    return {
        "v": CACHE_VERSION,
        "offset": 0,
        "in": 0,
        "cc": 0,
        "cr": 0,
        "out": 0,
        "fp": [0, 0],  # (st_ino, st_dev) — detects file replaced at the same path
        "recent": {},  # message.id -> last usage [in, cc, cr, out], FIFO-bounded
        "last_task": "",
    }


def stats_cache_path(transcript_path: str) -> str:
    import hashlib
    import tempfile

    digest = hashlib.md5(transcript_path.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"claude-statusline-{digest}.json")


def is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_stats_cache(cache_file: str) -> dict:
    """Load and fully validate the cache; any malformed field → fresh stats.

    Strict per-key validation matters: a cache that passes a partial check but
    breaks later in the scan would never be overwritten, bricking the line.
    """
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        return empty_stats()
    stats = empty_stats()
    if not isinstance(cache, dict) or cache.get("v") != CACHE_VERSION:
        return stats
    for key in ("offset",) + USAGE_KEYS:
        if not is_count(cache.get(key)):
            return empty_stats()
        stats[key] = cache[key]
    fp = cache.get("fp")
    if isinstance(fp, list) and len(fp) == 2 and all(is_count(v) for v in fp):
        stats["fp"] = fp
    recent = cache.get("recent")
    if isinstance(recent, dict):
        for mid, usage in recent.items():
            if (
                isinstance(mid, str)
                and isinstance(usage, list)
                and len(usage) == 4
                and all(is_count(v) for v in usage)
            ):
                stats["recent"][mid] = usage
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


def accumulate_entry(stats: dict, line: bytes, entry: dict) -> None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    usage = extract_usage(message)
    if usage is not None:
        mid = message.get("id")
        recent = stats["recent"]
        prev = recent.get(mid) if isinstance(mid, str) else None
        if prev is not None:
            # Same streamed response on another JSONL line: each line repeats
            # (possibly updated) usage, so replace — last line wins.
            for i, key in enumerate(USAGE_KEYS):
                stats[key] += usage[i] - prev[i]
            recent[mid] = usage
        else:
            for i, key in enumerate(USAGE_KEYS):
                stats[key] += usage[i]
            if isinstance(mid, str) and mid:
                if len(recent) >= RECENT_IDS_MAX:
                    recent.pop(next(iter(recent)))
                recent[mid] = usage
    if b'"Task"' in line:
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
                    stats["last_task"] = desc


def scan_transcript(transcript_path: str) -> dict:
    """Cumulative usage (deduplicated by message.id) + last Task description.

    The transcript is append-only JSONL, so completed work is cached by byte
    offset and only appended lines are parsed on subsequent renders.
    """
    cache_file = stats_cache_path(transcript_path)
    stats = load_stats_cache(cache_file)
    try:
        st = os.stat(transcript_path)
    except OSError:
        return empty_stats()
    fp = [int(getattr(st, "st_ino", 0) or 0), int(getattr(st, "st_dev", 0) or 0)]
    if stats["fp"] != fp or st.st_size < stats["offset"]:
        # New inode at the same path or truncation — cached offsets are invalid
        stats = empty_stats()
        stats["fp"] = fp
    if st.st_size == stats["offset"]:
        return stats
    try:
        with open(transcript_path, "rb") as f:
            f.seek(stats["offset"])
            offset = stats["offset"]
            for line in f:
                if not line.endswith(b"\n"):
                    break  # partial trailing write — picked up on the next render
                offset += len(line)
                # Cheap byte prefilter: only assistant entries carry usage/tool_use
                if b'"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                if isinstance(entry, dict) and entry.get("type") == "assistant":
                    try:
                        accumulate_entry(stats, line, entry)
                    except Exception:
                        continue  # one poison line must not stall the offset cache
        stats["offset"] = offset
        save_stats_cache(cache_file, stats)
    except OSError:
        pass
    return stats


# --- Cost ---

def cost_segment(data: dict, model: str, stats: dict) -> str:
    input_rate, output_rate = DEFAULT_RATE
    for key, rates in BASE_RATES.items():
        if key in model:
            input_rate, output_rate = rates
            break
    cost = (
        stats["in"] * input_rate
        + stats["cc"] * input_rate * 1.25
        + stats["cr"] * input_rate * 0.1
        + stats["out"] * output_rate
    ) / 1_000_000

    cost_obj = data.get("cost")
    official = duration_ms = None
    if isinstance(cost_obj, dict):
        official = as_number(cost_obj.get("total_cost_usd"))
        duration_ms = as_number(cost_obj.get("total_duration_ms"))

    burn = ""
    burn_base = official if official is not None else (cost if cost > 0 else None)
    if burn_base is not None and duration_ms and duration_ms >= BURN_RATE_MIN_MS:
        burn = f" ${burn_base / (duration_ms / 3_600_000):.2f}/h"

    if official is not None:
        return f"~${cost:.2f} (${official:.2f}{burn})"
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
    import time

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

def duration_segment(data: dict, transcript_path: str) -> str:
    # Payload duration survives resumed sessions; file birthtime is the fallback
    cost_obj = data.get("cost")
    ms = as_number(cost_obj.get("total_duration_ms")) if isinstance(cost_obj, dict) else None
    if ms and ms > 0:
        duration = int(ms // 1000)
    elif transcript_path and os.path.isfile(transcript_path):
        start_time = get_file_creation_time(transcript_path)
        if start_time <= 0:
            return "0m"
        import time

        duration = int(time.time() - start_time)
    else:
        return "0m"
    hours, remainder = divmod(duration, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"


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
        stats = scan_transcript(transcript_path)
    else:
        stats = empty_stats()

    return (
        f"{context_segment(data)}"
        f" | {MAGENTA}{duration_segment(data, transcript_path)}{RESET}"
        f" | {YELLOW}{cost_segment(data, model, stats)}{RESET}"
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
    safe_print(line)


if __name__ == "__main__":
    # Ensure stdout can handle Unicode on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
