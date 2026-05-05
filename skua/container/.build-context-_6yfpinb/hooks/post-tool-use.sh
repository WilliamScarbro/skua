#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Claude Code PostToolUse hook — records that the agent is idle between tool calls.
#
# Claude Code calls this after each tool invocation completes.

STATUS_FILE="/tmp/skua-agent-status"
printf '{"state":"idle","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"
exit 0
