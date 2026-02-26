#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Background activity monitor for the Codex agent.
#
# Runs as a daemon started at container startup. Tracks the Codex node
# process and its children to infer thinking vs idle state.
#
# Codex is installed as a Node.js package, so the process name is "node"
# with a path containing "codex" in the argv.
#
# Heuristic: if the Codex process has child processes, it is executing a
# tool (shell command, file read, etc.) and is "thinking". With no children
# and no open established connections it is waiting for user input ("idle").

STATUS_FILE="${1:-/tmp/skua-agent-status}"
WAS_RUNNING=0

while true; do
    sleep 1

    # Find the Codex node process by matching its argv path
    CODEX_PID=$(pgrep -f "node.*codex" 2>/dev/null | head -1)

    if [ -z "$CODEX_PID" ]; then
        if [ "$WAS_RUNNING" -eq 1 ]; then
            # Codex just exited — mark as done
            printf '{"state":"done","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"
            WAS_RUNNING=0
        fi
        continue
    fi

    WAS_RUNNING=1

    # Count direct child processes — tool execution spawns shell subprocesses
    CHILDREN=$(pgrep -P "$CODEX_PID" 2>/dev/null | wc -l)
    if [ "$CHILDREN" -gt 0 ]; then
        printf '{"state":"thinking","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"
    else
        printf '{"state":"idle","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"
    fi
done
