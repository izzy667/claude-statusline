#!/bin/bash

# Read JSON input
input=$(cat)

# Extract data
model=$(echo "$input" | jq -r '.model.display_name')
cwd=$(echo "$input" | jq -r '.workspace.current_dir')
transcript_path=$(echo "$input" | jq -r '.transcript_path')

# Calculate context usage
usage=$(echo "$input" | jq '.context_window.current_usage')
if [ "$usage" != "null" ]; then
    current=$(echo "$usage" | jq '.input_tokens + .cache_creation_input_tokens + .cache_read_input_tokens')
    size=$(echo "$input" | jq '.context_window.context_window_size')
    context_pct=$((current * 100 / size))
    current_k=$((current / 1000))
    size_k=$((size / 1000))
    context_info=$(printf "%dK/%dK (%d%%)" "$current_k" "$size_k" "$context_pct")
else
    context_info="0%"
fi

# Cumulative token usage from transcript (includes all resumes, cache breakdown)
total_in=0; total_out=0; total_cache_create=0; total_cache_read=0
if [ -f "$transcript_path" ]; then
    read -r total_in total_cache_create total_cache_read total_out < <(
        jq -r 'select(.type=="assistant" and .message.usage) | .message.usage |
            "\(.input_tokens // 0) \(.cache_creation_input_tokens // 0) \(.cache_read_input_tokens // 0) \(.output_tokens // 0)"' \
            "$transcript_path" 2>/dev/null \
        | awk '{i+=$1; c+=$2; r+=$3; o+=$4} END{print i+0, c+0, r+0, o+0}'
    )
fi
total_all_input=$((total_in + total_cache_create + total_cache_read))

# Token info display with auto suffix
format_tokens() {
    local tokens=$1
    if [ "$tokens" -ge 1000000 ]; then
        printf "%.1fM" "$(echo "scale=2; $tokens / 1000000" | bc)" | sed 's/\.0M$/M/'
    elif [ "$tokens" -ge 1000 ]; then
        printf "%.1fK" "$(echo "scale=1; $tokens / 1000" | bc)" | sed 's/\.0K$/K/'
    else
        echo "$tokens"
    fi
}

token_in_display=$(format_tokens "$total_all_input")
token_out_display=$(format_tokens "$total_out")

# Cache hit percentage (cumulative across session)
cache_info=""
if [ "$total_all_input" -gt 0 ] && [ "$total_cache_read" -gt 0 ]; then
    cache_pct=$(echo "scale=0; $total_cache_read * 100 / $total_all_input" | bc)
    cache_info=" C:${cache_pct}%"
fi

token_info="↓${token_in_display} ↑${token_out_display}${cache_info}"

# Theoretical API cost with cache-aware pricing (rates per million tokens)
if [[ "$model" == *"Opus"* ]]; then
    input_rate=15;    output_rate=75;   cache_write_rate=18.75; cache_read_rate=1.50
elif [[ "$model" == *"Sonnet"* ]]; then
    input_rate=3;     output_rate=15;   cache_write_rate=3.75;  cache_read_rate=0.30
elif [[ "$model" == *"Haiku"* ]]; then
    input_rate=0.80;  output_rate=4;    cache_write_rate=1.00;  cache_read_rate=0.08
else
    input_rate=3;     output_rate=15;   cache_write_rate=3.75;  cache_read_rate=0.30
fi

cost=$(echo "scale=4; ($total_in * $input_rate + $total_cache_create * $cache_write_rate + $total_cache_read * $cache_read_rate + $total_out * $output_rate) / 1000000" | bc)
cost_display=$(printf "~\$%.2f" "$cost")

# Rate limits (subscription users only)
rate_info=""
five_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
seven_pct=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
if [ -n "$five_pct" ] || [ -n "$seven_pct" ]; then
    parts=""
    if [ -n "$five_pct" ]; then
        parts="5h:$(printf '%.0f' "$five_pct")%"
    fi
    if [ -n "$seven_pct" ]; then
        [ -n "$parts" ] && parts="$parts "
        parts="${parts}7d:$(printf '%.0f' "$seven_pct")%"
    fi
    rate_info=" | $(printf '\033[0;31m')${parts}$(printf '\033[0m')"
fi

# Session duration
if [ -f "$transcript_path" ]; then
    start_time=$(stat -f %B "$transcript_path" 2>/dev/null || echo "0")

    if [ "$start_time" != "0" ]; then
        current_time=$(date +%s)
        duration=$((current_time - start_time))

        hours=$((duration / 3600))
        minutes=$(((duration % 3600) / 60))

        if [ $hours -gt 0 ]; then
            time_display=$(printf "%dh%dm" "$hours" "$minutes")
        else
            time_display=$(printf "%dm" "$minutes")
        fi
    else
        time_display="0m"
    fi
else
    time_display="0m"
fi

# Git branch and dirty status
if [ -d "$cwd/.git" ]; then
    branch=$(cd "$cwd" && git -c core.fileMode=false -c advice.detachedHead=false rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ -n "$branch" ]; then
        # Check git dirty status
        git_status=$(cd "$cwd" && git -c core.fileMode=false status --porcelain 2>/dev/null)
        if [ -n "$git_status" ]; then
            # Count added, modified, deleted files
            added=$(echo "$git_status" | grep -c '^A\|^??' || echo "0")
            modified=$(echo "$git_status" | grep -c '^.M\|^ M' || echo "0")
            deleted=$(echo "$git_status" | grep -c '^.D\|^ D' || echo "0")

            dirty_status=" ●"
            [ "$added" -gt 0 ] && dirty_status="${dirty_status} +${added}"
            [ "$modified" -gt 0 ] && dirty_status="${dirty_status} ~${modified}"
            [ "$deleted" -gt 0 ] && dirty_status="${dirty_status} -${deleted}"
        else
            dirty_status=" ○"
        fi

        git_info=" | $(printf '\033[0;36m')${branch}${dirty_status}$(printf '\033[0m')"
    else
        git_info=""
    fi
else
    git_info=""
fi

# Extract current task from transcript using JSON-based approach
task_info=""
if [ -f "$transcript_path" ]; then
    # Primary: Extract summary from first line of JSONL
    task_name=$(head -1 "$transcript_path" 2>/dev/null | jq -r '.summary // empty')

    # Fallback: Get latest Task tool description from JSONL
    if [ -z "$task_name" ]; then
        task_name=$(jq -r '.message.content[]? | select(.type=="tool_use" and .name=="Task").input.description // empty' "$transcript_path" 2>/dev/null | tail -1)
    fi

    # If we found a task name, truncate to ~45 chars and add to display
    if [ -n "$task_name" ]; then
        # Truncate to 45 characters, adding ellipsis if needed
        if [ ${#task_name} -gt 45 ]; then
            task_name="${task_name:0:42}..."
        fi
        task_info=" - $task_name"
    fi
fi

# Compact format: Context | Time | Cost | Model | Token info | Branch - Task
printf '\033[0;32m%s\033[0m | \033[0;35m%s\033[0m | \033[0;33m%s\033[0m | \033[0;34m%s\033[0m | \033[0;36m%s\033[0m%s%s%s' \
    "$context_info" "$time_display" "$cost_display" "$model" "$token_info" "$rate_info" "$git_info" "$task_info"