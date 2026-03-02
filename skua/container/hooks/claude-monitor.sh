#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Background activity monitor for the Claude agent.
#
# Claude Code already provides PreToolUse/PostToolUse/Stop hooks that write
# "thinking", "idle", and "done" to /tmp/skua-agent-status.  However,
# between tool calls Claude spends significant time waiting for the LLM
# API response — during this phase PostToolUse has already set "idle" even
# though the agent is actively working.
#
# This monitor fills that gap: it uses tcpdump to count recent HTTPS
# packets and, when traffic is detected while the state is "idle",
# promotes the status to "api_activity" so that `skua list` shows the
# agent as active.  Hook-written states ("thinking", "done") are never
# overridden.

STATUS_FILE="${1:-/tmp/skua-agent-status}"
API_ACTIVITY_WINDOW="${SKUA_CLAUDE_API_ACTIVITY_WINDOW:-30}"
API_IDLE_THRESHOLD="${SKUA_CLAUDE_API_IDLE_THRESHOLD:-2}"
API_PORTS="${SKUA_CLAUDE_API_PORTS:-443}"
API_EVENTS_FILE="/tmp/skua-claude-api-events"
TCPDUMP_PID=""
LOG_FILE="/tmp/monitor_logs"
LAST_PAYLOAD=""

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

set_status() {
    local state="$1"
    local hits="$2"
    local payload
    if [ -n "$hits" ]; then
        payload=$(printf '{"state":"%s","hits":%d,"window":%d,"ts":%d}\n' \
            "$state" "$hits" "$API_ACTIVITY_WINDOW" "$(date +%s)")
    else
        payload=$(printf '{"state":"%s","ts":%d}\n' "$state" "$(date +%s)")
    fi
    if [ "$payload" != "$LAST_PAYLOAD" ]; then
        printf '%s' "$payload" > "$STATUS_FILE"
        LAST_PAYLOAD="$payload"
        log "status_update state=${state} hits=${hits:-} payload=${payload}"
    fi
}

_current_state() {
    grep -o '"state":"[^"]*"' "$STATUS_FILE" 2>/dev/null | cut -d'"' -f4
}

_tcpdump_filter() {
    local port filter first=1
    filter="tcp and ("
    for port in $API_PORTS; do
        if [ "$first" -eq 1 ]; then
            filter="${filter}port ${port}"
            first=0
        else
            filter="${filter} or port ${port}"
        fi
    done
    filter="${filter})"
    printf '%s' "$filter"
}

_start_tcpdump() {
    [ -n "$TCPDUMP_PID" ] && kill -0 "$TCPDUMP_PID" 2>/dev/null && return 0
    if ! command -v tcpdump >/dev/null 2>&1; then
        log "tcpdump_missing"
        return 1
    fi
    rm -f "$API_EVENTS_FILE"
    local filter
    filter="$(_tcpdump_filter)"
    log "tcpdump_start filter=${filter}"
    (tcpdump -l -n -i any -q -U $filter 2>>"$LOG_FILE" | while read -r _; do
        date +%s >> "$API_EVENTS_FILE"
        log "tcpdump_packet api_event_recorded"
    done) &
    TCPDUMP_PID=$!
    log "tcpdump_pid=${TCPDUMP_PID}"
    return 0
}

_recent_api_hits() {
    local now_ts="$1"
    local cutoff=$((now_ts - API_ACTIVITY_WINDOW + 1))
    [ -f "$API_EVENTS_FILE" ] || { printf '0'; return; }
    awk -v cutoff="$cutoff" '$1 >= cutoff { print $1 }' \
        "$API_EVENTS_FILE" > "${API_EVENTS_FILE}.tmp" 2>/dev/null || { printf '0'; return; }
    mv "${API_EVENTS_FILE}.tmp" "$API_EVENTS_FILE"
    wc -l < "$API_EVENTS_FILE" | tr -d ' '
}

log "claude_monitor_start status_file=${STATUS_FILE} api_ports=${API_PORTS} idle_threshold=${API_IDLE_THRESHOLD} activity_window=${API_ACTIVITY_WINDOW}"

_start_tcpdump || log "tcpdump_start_failed"

while true; do
    sleep 1

    # Never override hook-managed states: hooks have ground truth during
    # tool execution ("thinking") and after the agent finishes ("done").
    CURRENT=$(_current_state)
    if [ "$CURRENT" = "thinking" ] || [ "$CURRENT" = "done" ]; then
        continue
    fi

    NOW_TS=$(date +%s)
    HITS=$(_recent_api_hits "$NOW_TS")
    log "poll current_state=${CURRENT} api_hits=${HITS}"

    if [ "$HITS" -lt "$API_IDLE_THRESHOLD" ]; then
        continue  # Not enough recent traffic to override idle
    fi

    set_status "api_activity" "$HITS"
done
