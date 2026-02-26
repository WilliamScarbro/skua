#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Claude Code PreToolUse hook — records that the agent is actively using a tool.
#
# Claude Code calls this before each tool invocation, passing JSON on stdin:
#   {"tool_name": "Bash", "tool_input": {...}, "session_id": "...", ...}
#
# Must exit 0 to allow the tool use to proceed.

STATUS_FILE="/tmp/skua-agent-status"

input=$(cat)
tool=$(printf '%s' "$input" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null)

printf '{"state":"thinking","tool":"%s","ts":%d}\n' "${tool}" "$(date +%s)" > "$STATUS_FILE"
exit 0
