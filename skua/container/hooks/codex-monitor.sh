#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Background activity monitor for the Codex agent.
#
# Runs as a daemon started at container startup. Tracks the Codex runtime
# process to infer thinking vs idle state during a live conversation.
#
# Codex is installed via a Node.js launcher, but the launcher keeps a
# long-lived child `codex` binary alive even while idle. We therefore anchor
# on the native `codex` process when present and only treat descendants of
# that process as active tool work.
#
# Heuristic:
# - Track HTTPS API usage via tcpdump in a rolling 30-second window.
# - "processing" when Codex has active subprocesses.
# - "api_activity" with a hit count when recent API usage is above the idle threshold.
# - "idle" when recent API usage is below the idle threshold.

STATUS_FILE="${1:-/tmp/skua-agent-status}"
WAS_RUNNING=0
LAST_PAYLOAD=""
API_ACTIVITY_WINDOW="${SKUA_CODEX_API_ACTIVITY_WINDOW:-30}"
API_IDLE_THRESHOLD="${SKUA_CODEX_API_IDLE_THRESHOLD:-100}"
API_PORTS="${SKUA_CODEX_API_PORTS:-443}"
API_EVENTS_FILE="/tmp/skua-codex-api-events"
TCPDUMP_PID=""
LOG_FILE="/tmp/monitor_logs"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

set_status() {
    local state="$1"
    local hits="$2"
    local payload
    if [ -n "$hits" ]; then
        payload=$(printf '{"state":"%s","hits":%d,"window":%d,"ts":%d}\n' "$state" "$hits" "$API_ACTIVITY_WINDOW" "$(date +%s)")
    else
        payload=$(printf '{"state":"%s","ts":%d}\n' "$state" "$(date +%s)")
    fi
    if [ "$payload" != "$LAST_PAYLOAD" ]; then
        printf '%s' "$payload" > "$STATUS_FILE"
        LAST_PAYLOAD="$payload"
        log "status_update state=${state} hits=${hits:-} payload=${payload}"
    fi
}

_tcpdump_filter() {
    local port
    local filter="tcp and ("
    local first=1
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

    [ -f "$API_EVENTS_FILE" ] || {
        printf '0'
        return 0
    }

    awk -v cutoff="$cutoff" '
        $1 >= cutoff { print $1 }
    ' "$API_EVENTS_FILE" > "${API_EVENTS_FILE}.tmp" 2>/dev/null || {
        printf '0'
        return 0
    }

    mv "${API_EVENTS_FILE}.tmp" "$API_EVENTS_FILE"
    wc -l < "$API_EVENTS_FILE" | tr -d ' '
}

_find_codex_pid() {
    local pid=""

    # Prefer the native Codex binary. The node wrapper keeps this as a stable
    # child, so tracking the wrapper misclassifies idle as processing.
    pid=$(pgrep -f '/codex/codex( |$)' 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        printf '%s' "$pid"
        return 0
    fi

    pid=$(pgrep -f 'node.*codex' 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        printf '%s' "$pid"
        return 0
    fi

    return 1
}

log "codex_monitor_start status_file=${STATUS_FILE} api_ports=${API_PORTS} idle_threshold=${API_IDLE_THRESHOLD} activity_window=${API_ACTIVITY_WINDOW}"

while true; do
    sleep 1

    # Prefer the native Codex runtime and fall back to the node launcher.
    CODEX_PID=$(_find_codex_pid)
    log "poll codex_pid=${CODEX_PID:-none}"

    if [ -z "$CODEX_PID" ]; then
        if [ "$WAS_RUNNING" -eq 1 ]; then
            # Codex just exited — mark as done
            set_status "done"
            WAS_RUNNING=0
        fi
        continue
    fi

    if [ "$WAS_RUNNING" -eq 0 ]; then
        WAS_RUNNING=1
        _start_tcpdump || log "tcpdump_start_failed"
    fi

    # Count descendants (children + deeper subprocess tree)
    DESCENDANTS=$(pgrep -P "$CODEX_PID" -d ' ' 2>/dev/null || true)
    if [ -n "$DESCENDANTS" ]; then
        QUEUE="$DESCENDANTS"
        DESC_COUNT=0
        while [ -n "$QUEUE" ]; do
            PID="${QUEUE%% *}"
            if [ "$QUEUE" = "$PID" ]; then
                QUEUE=""
            else
                QUEUE="${QUEUE#* }"
            fi
            [ -n "$PID" ] || continue
            DESC_COUNT=$((DESC_COUNT + 1))
            KIDS=$(pgrep -P "$PID" -d ' ' 2>/dev/null || true)
            if [ -n "$KIDS" ]; then
                if [ -n "$QUEUE" ]; then
                    QUEUE="$QUEUE $KIDS"
                else
                    QUEUE="$KIDS"
                fi
            fi
        done
    else
        DESC_COUNT=0
    fi

    # If Codex has subprocesses, assume active even without recent API traffic.
    if [ "$DESC_COUNT" -gt 0 ]; then
        set_status "processing"
        continue
    fi

    NOW_TS=$(date +%s)
    HITS=$(_recent_api_hits "$NOW_TS")
    log "api_hits_window=${HITS}"
    if [ "$HITS" -lt "$API_IDLE_THRESHOLD" ]; then
        set_status "idle"
        continue
    fi

    set_status "api_activity" "$HITS"
done
