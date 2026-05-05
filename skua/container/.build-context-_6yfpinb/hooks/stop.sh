#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Claude Code Stop / SubagentStop hook — records that the agent has finished.
#
# Claude Code calls this when the agent completes its task or a subagent exits.

STATUS_FILE="/tmp/skua-agent-status"
printf '{"state":"done","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"
exit 0
