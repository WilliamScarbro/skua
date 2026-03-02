#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Claude Code agent monitoring setup — runs at container startup.
#
# Merges skua activity-tracking hooks into ~/.claude/settings.local.json so
# that Claude Code reports its state to /tmp/skua-agent-status.  The merge
# is idempotent: re-running this script on subsequent container starts does
# not duplicate hook entries.
#
# Requires $AUTH_DIR to be set (done by entrypoint.sh before this is called).

HOOKS_DIR="/home/dev/.skua/hooks"
STATUS_FILE="/tmp/skua-agent-status"

# Initialise status file so skua list shows "idle" from first boot
printf '{"state":"idle","ts":%d}\n' "$(date +%s)" > "$STATUS_FILE"

# Merge skua hooks into settings.local.json using inline Python (always
# available in skua images).  settings.local.json is machine-local by
# convention — a safe place to add monitoring config without touching
# the user's own settings.json.
python3 - "${AUTH_DIR:-/home/dev/.claude}" "$HOOKS_DIR" << 'PYEOF'
import json
import sys
from pathlib import Path

auth_dir = Path(sys.argv[1])
hooks_dir = sys.argv[2]

settings_path = auth_dir / "settings.local.json"

settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        pass  # corrupt or empty — start fresh

skua_hooks = {
    "PreToolUse": [{
        "matcher": ".*",
        "hooks": [{"type": "command", "command": f"{hooks_dir}/pre-tool-use.sh"}],
    }],
    "PostToolUse": [{
        "matcher": ".*",
        "hooks": [{"type": "command", "command": f"{hooks_dir}/post-tool-use.sh"}],
    }],
    "Stop": [{
        "hooks": [{"type": "command", "command": f"{hooks_dir}/stop.sh"}],
    }],
    "SubagentStop": [{
        "hooks": [{"type": "command", "command": f"{hooks_dir}/stop.sh"}],
    }],
}

existing_hooks = settings.setdefault("hooks", {})
added = []
for event, new_entries in skua_hooks.items():
    existing_event = existing_hooks.setdefault(event, [])
    # Idempotency check: skip if any entry already references our hooks dir
    already_added = any(
        hooks_dir in h.get("command", "")
        for entry in existing_event
        for h in entry.get("hooks", [])
    )
    if not already_added:
        existing_event.extend(new_entries)
        added.append(event)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2) + "\n")

if added:
    print(f"[OK] Claude Code monitoring hooks added: {', '.join(added)}")
else:
    print("[OK] Claude Code monitoring hooks already configured")
PYEOF

# Start background API activity monitor so that the inter-tool-call LLM
# inference phase is visible in `skua list` rather than appearing as "idle".
nohup bash "$HOOKS_DIR/claude-monitor.sh" "$STATUS_FILE" > /dev/null 2>&1 &
echo "[OK] Claude activity monitor started (pid $!)"
