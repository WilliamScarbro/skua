#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Codex agent monitoring setup — runs at container startup.
#
# Starts a lightweight background daemon that tracks the Codex process and
# writes its inferred activity state to /tmp/skua-agent-status.
#
# Because Codex does not expose a formal hook API, we use process-level
# monitoring: when the Codex node process has child processes (running shell
# commands or other tools) the state is "thinking"; otherwise "idle".

HOOKS_DIR="/home/dev/.skua/hooks"
STATUS_FILE="/tmp/skua-agent-status"

# Initialise status file so skua list shows "idle" from first boot
printf '{"state":"idle","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"

# Start background process monitor
nohup bash "$HOOKS_DIR/codex-monitor.sh" "$STATUS_FILE" > /dev/null 2>&1 &
echo "[OK] Codex activity monitor started (pid $!)"
