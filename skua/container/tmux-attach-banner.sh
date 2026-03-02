#!/usr/bin/env bash
set -eu

SESSION_NAME="${1:-skua}"
INFO_FILE="/tmp/skua-entrypoint-info.txt"
BANNER_SHOWN_FLAG="/tmp/skua-banner-shown"

# Only show once per container lifecycle
[ ! -f "$BANNER_SHOWN_FLAG" ] || exit 0

# On first start, entrypoint.sh may still be running when we're called.
# Wait up to 5 seconds for it to write the info file.
waited=0
while [ ! -f "$INFO_FILE" ] && [ "$waited" -lt 5 ]; do
    sleep 1
    waited=$((waited + 1))
done
[ -f "$INFO_FILE" ] || exit 0
touch "$BANNER_SHOWN_FLAG"

pane_tty=$(tmux list-panes -t "$SESSION_NAME" -F "#{pane_tty}" 2>/dev/null | head -1)
pane_id=$(tmux list-panes -t "$SESSION_NAME" -F "#{pane_id}" 2>/dev/null | head -1)

if [ -n "$pane_tty" ] && [ -c "$pane_tty" ]; then
    # Leading newline so the banner starts on its own line after the prompt
    printf '\n' > "$pane_tty"
    cat "$INFO_FILE" > "$pane_tty"
    printf '\n' > "$pane_tty"
    # Submit an empty command so bash redraws the prompt without user hitting Enter
    tmux send-keys -t "${pane_id:-$SESSION_NAME}" "" Enter
fi
