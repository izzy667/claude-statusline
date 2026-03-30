#!/usr/bin/env python3
"""Claude Code status line — cross-platform (Windows/macOS/Linux)."""

import json
import os
import sys


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


def main() -> None:
    raw = sys.stdin.read()
    data = json.loads(raw)

    # DEBUG: dump raw input to file for inspection
    debug_path = os.path.join(os.path.dirname(__file__), "debug_input.json")
    with open(debug_path, "w", encoding="utf-8") as df:
        json.dump(data, df, indent=2)

    model = data.get("model", {}).get("display_name", "Unknown")
    # Compact context size hints: "(1M context)" → "1M"
    for tag in ("(1M context)", "(200K context)"):
        if tag in model:
            model = model.replace(tag, tag[1:tag.index(" ")])
            break
    cwd = data.get("workspace", {}).get("current_dir", ".")
    transcript_path = data.get("transcript_path", "")

    # --- Context window usage ---
    usage = data.get("context_window", {}).get("current_usage")
    if usage:
        current = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        size = data.get("context_window", {}).get("context_window_size", 1)
        context_pct = current * 100 // size
        context_info = f"{current // 1000}K/{size // 1000}K ({context_pct}%)"
    else:
        context_info = "0%"

    # --- Cumulative token usage from transcript ---
    total_in = 0
    total_out = 0
    total_cache_create = 0
    total_cache_read = 0

    if transcript_path and os.path.isfile(transcript_path):
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    u = entry.get("message", {}).get("usage")
                    if not u:
                        continue
                    total_in += u.get("input_tokens", 0)
                    total_cache_create += u.get("cache_creation_input_tokens", 0)
                    total_cache_read += u.get("cache_read_input_tokens", 0)
                    total_out += u.get("output_tokens", 0)
        except OSError:
            pass

    total_all_input = total_in + total_cache_create + total_cache_read

    token_in_display = format_tokens(total_all_input)
    token_out_display = format_tokens(total_out)

    # Cache hit percentage
    cache_info = ""
    if total_all_input > 0 and total_cache_read > 0:
        cache_pct = total_cache_read * 100 // total_all_input
        cache_info = f" C:{cache_pct}%"

    token_info = f"\u2193{token_in_display} \u2191{token_out_display}{cache_info}"

    # --- Theoretical API cost (per million tokens) ---
    # Base rates: (input, output); cache: write=1.25x input, read=0.1x input
    base_rates = {
        "Opus": (5, 25),
        "Sonnet": (3, 15),
        "Haiku": (1, 5),
    }
    input_rate, output_rate = (3, 15)
    for key, r in base_rates.items():
        if key in model:
            input_rate, output_rate = r
            break
    cache_write_rate = input_rate * 1.25
    cache_read_rate = input_rate * 0.1

    cost = (
        total_in * input_rate
        + total_cache_create * cache_write_rate
        + total_cache_read * cache_read_rate
        + total_out * output_rate
    ) / 1_000_000
    official_cost = data.get("cost", {}).get("total_cost_usd")
    if official_cost is not None:
        cost_display = f"~${cost:.2f} (${official_cost:.0f})" if official_cost >= 1 else f"~${cost:.2f} (${official_cost:.2f})"
    else:
        cost_display = f"~${cost:.2f}"

    # --- Rate limits (dynamic color: default <50%, orange 50-79%, red 80%+) ---
    def rate_color(pct: float) -> str:
        if pct >= 80:
            return "\033[0;31m"   # red
        if pct >= 50:
            return "\033[0;33m"   # orange/yellow
        return "\033[0;37m"       # gray (default)

    def format_remaining(resets_at: int | float | None) -> str:
        if not resets_at:
            return ""
        import time, math
        remaining = int(resets_at - time.time())
        if remaining <= 0:
            return ""
        if remaining < 3600:
            return f"{remaining // 60}m"
        hours = math.ceil(remaining / 3600)
        if hours > 10:
            return f"{math.ceil(remaining / 86400)}d"
        return f"{hours}h"

    rate_info = ""
    rl = data.get("rate_limits", {})
    five = rl.get("five_hour", {})
    seven = rl.get("seven_day", {})
    five_pct = five.get("used_percentage")
    seven_pct = seven.get("used_percentage")
    if five_pct is not None or seven_pct is not None:
        parts = []
        if five_pct is not None:
            c = rate_color(five_pct)
            remaining = format_remaining(five.get("resets_at"))
            label = remaining if remaining else "5h"
            parts.append(f"{c}{label}:{five_pct:.0f}%\033[0m")
        if seven_pct is not None:
            c = rate_color(seven_pct)
            remaining = format_remaining(seven.get("resets_at"))
            label = remaining if remaining else "7d"
            parts.append(f"{c}{label}:{seven_pct:.0f}%\033[0m")
        rate_info = f" | {' '.join(parts)}"

    # --- Session duration ---
    time_display = "0m"
    if transcript_path and os.path.isfile(transcript_path):
        import time

        start_time = get_file_creation_time(transcript_path)
        if start_time > 0:
            duration = int(time.time() - start_time)
            hours, remainder = divmod(duration, 3600)
            minutes = remainder // 60
            time_display = f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"

    # --- Git branch & dirty status ---
    git_info = ""
    git_dir = os.path.join(cwd, ".git")
    if os.path.isdir(git_dir):
        import subprocess

        try:
            branch = (
                subprocess.check_output(
                    ["git", "-c", "core.fileMode=false", "-c", "advice.detachedHead=false",
                     "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=cwd, stderr=subprocess.DEVNULL, text=True,
                )
                .strip()
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            branch = ""

        if branch:
            try:
                git_status = (
                    subprocess.check_output(
                        ["git", "-c", "core.fileMode=false", "status", "--porcelain"],
                        cwd=cwd, stderr=subprocess.DEVNULL, text=True,
                    )
                    .strip()
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                git_status = ""

            if git_status:
                added = modified = deleted = 0
                for st_line in git_status.splitlines():
                    if st_line.startswith("A ") or st_line.startswith("??"):
                        added += 1
                    if len(st_line) >= 2 and st_line[1] == "M":
                        modified += 1
                    if len(st_line) >= 2 and st_line[1] == "D":
                        deleted += 1
                dirty = " \u25cf"
                if added > 0:
                    dirty += f" +{added}"
                if modified > 0:
                    dirty += f" ~{modified}"
                if deleted > 0:
                    dirty += f" -{deleted}"
            else:
                dirty = " \u25cb"

            git_info = f" | \033[0;36m{branch}{dirty}\033[0m"

    # --- Task info from transcript ---
    task_info = ""
    if transcript_path and os.path.isfile(transcript_path):
        task_name = ""
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if first_line:
                    try:
                        first = json.loads(first_line)
                        task_name = first.get("summary", "")
                    except json.JSONDecodeError:
                        pass

                if not task_name:
                    f.seek(0)
                    last_task = ""
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        for block in entry.get("message", {}).get("content", []):
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_use"
                                and block.get("name") == "Task"
                            ):
                                desc = block.get("input", {}).get("description", "")
                                if desc:
                                    last_task = desc
                    task_name = last_task
        except OSError:
            pass

        if task_name:
            if len(task_name) > 45:
                task_name = task_name[:42] + "..."
            task_info = f" - {task_name}"

    # --- Output ---
    print(
        f"\033[0;32m{context_info}\033[0m"
        f" | \033[0;35m{time_display}\033[0m"
        f" | \033[0;33m{cost_display}\033[0m"
        f" | \033[0;34m{model}\033[0m"
        f" | \033[0;36m{token_info}\033[0m"
        f"{rate_info}{git_info}{task_info}",
        end="",
    )


if __name__ == "__main__":
    # Ensure stdout can handle Unicode on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
