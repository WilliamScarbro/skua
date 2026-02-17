#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
set -e

AGENT_NAME="${SKUA_AGENT_NAME:-agent}"
AGENT_COMMAND="${SKUA_AGENT_COMMAND:-$AGENT_NAME}"
AGENT_LOGIN_COMMAND="${SKUA_AGENT_LOGIN_COMMAND:-$AGENT_COMMAND login}"
AUTH_DIR_REL="${SKUA_AUTH_DIR:-.claude}"
AUTH_DIR="/home/dev/${AUTH_DIR_REL#/}"
AUTH_FILES_RAW="${SKUA_AUTH_FILES:-}"
PROJECT_DIR="${SKUA_PROJECT_DIR:-/home/dev/project}"
IMAGE_REQUEST_FILE="${SKUA_IMAGE_REQUEST_FILE:-$PROJECT_DIR/.skua/image-request.yaml}"
PREP_GUIDE_FILE="${SKUA_PREP_GUIDE_FILE:-$PROJECT_DIR/.skua/PREP.md}"

echo "============================================"
echo "  skua — Dockerized Coding Agent"
echo "============================================"
echo ""
echo "Agent: ${AGENT_NAME}"
echo "Auth:  ${AUTH_DIR_REL}"
echo ""

# ── Configure git identity from env vars ─────────────────────────────
if [ -n "$GIT_AUTHOR_NAME" ]; then
    git config --global user.name "$GIT_AUTHOR_NAME"
    git config --global user.email "$GIT_AUTHOR_EMAIL"
    echo "[OK] Git: $GIT_AUTHOR_NAME <$GIT_AUTHOR_EMAIL>"
else
    echo "[--] Git identity not set"
fi

# ── SSH key pair (read-only mount -> local copy with correct perms) ───
if [ -d /home/dev/.ssh-mount ] && [ "$(ls -A /home/dev/.ssh-mount 2>/dev/null)" ]; then
    mkdir -p /home/dev/.ssh
    cp /home/dev/.ssh-mount/* /home/dev/.ssh/ 2>/dev/null || true
    chmod 700 /home/dev/.ssh
    chmod 600 /home/dev/.ssh/* 2>/dev/null || true
    SSH_KEY=$(find /home/dev/.ssh -maxdepth 1 -type f ! -name '*.pub' ! -name 'known_hosts' | head -1)
    if [ -n "$SSH_KEY" ]; then
        KH_OPT=""
        [ -f /home/dev/.ssh/known_hosts ] && KH_OPT="-o UserKnownHostsFile=/home/dev/.ssh/known_hosts"
        export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new $KH_OPT"
        echo "[OK] SSH key loaded: $(basename "$SSH_KEY")"
    else
        echo "[--] SSH mount found but no private key detected"
    fi
else
    echo "[--] No SSH key mounted"
fi

# ── Fix volume ownership (Docker creates named volumes as root) ───────
DEV_GROUP="$(id -gn dev)"
mkdir -p "$AUTH_DIR"
sudo chown -R dev:"$DEV_GROUP" "$AUTH_DIR"

# ── Seed Claude config defaults into persistent volume ──────────────
if [ "$AUTH_DIR_REL" = ".claude" ] && [ -d /home/dev/.claude-defaults ]; then
    for src in /home/dev/.claude-defaults/*; do
        [ -f "$src" ] || continue
        dest="${AUTH_DIR}/$(basename "$src")"
        [ -f "$dest" ] || cp "$src" "$dest"
    done
fi

# ── Symlink ~/.claude.json into the persistent volume ────────────────
# Claude Code reads/writes ~/.claude.json (account metadata, onboarding
# state, etc.) which lives OUTSIDE ~/.claude/. We store the real file
# inside the persistent volume and symlink it so writes persist.
if [ "$AUTH_DIR_REL" = ".claude" ]; then
    rm -f /home/dev/.claude.json
    if [ ! -f "${AUTH_DIR}/.claude.json" ]; then
        # First run: create an empty JSON object so Claude can populate it
        echo '{}' > "${AUTH_DIR}/.claude.json"
    fi
    ln -sf "${AUTH_DIR}/.claude.json" /home/dev/.claude.json
fi

# ── Shell aliases ────────────────────────────────────────────────────
if [ "$AGENT_COMMAND" = "claude" ]; then
    echo "alias claude-dsp='claude --dangerously-skip-permissions'" >> /home/dev/.bashrc
fi

# ── Check tool availability ──────────────────────────────────────────
if command -v "$AGENT_COMMAND" &>/dev/null; then
    echo "[OK] ${AGENT_NAME} available"
else
    echo "[!!] ${AGENT_NAME} command not found: ${AGENT_COMMAND}"
fi

# ── Check auth status ────────────────────────────────────────────────
NEEDS_LOGIN=()
PRIMARY_AUTH_FILE=""
IFS=',' read -r -a AUTH_FILES <<< "$AUTH_FILES_RAW"
if [ ${#AUTH_FILES[@]} -gt 0 ] && [ -n "${AUTH_FILES[0]}" ]; then
    PRIMARY_AUTH_FILE="$AUTH_DIR/${AUTH_FILES[0]}"
fi

if [ -n "$PRIMARY_AUTH_FILE" ] && [ -f "$PRIMARY_AUTH_FILE" ]; then
    echo "[OK] ${AGENT_NAME} authenticated (persistent)"
else
    if [ -n "$PRIMARY_AUTH_FILE" ]; then
        echo "[--] ${AGENT_NAME} not logged in (${PRIMARY_AUTH_FILE} missing)"
    else
        echo "[--] ${AGENT_NAME} auth file not configured"
    fi
    NEEDS_LOGIN+=("$AGENT_LOGIN_COMMAND")
fi

echo ""

# ── Project ──────────────────────────────────────────────────────────
if [ -d "$PROJECT_DIR" ] && [ "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]; then
    echo "Project: ${PROJECT_DIR}"
    cd "$PROJECT_DIR"
    if [ -f "$IMAGE_REQUEST_FILE" ]; then
        echo "Image prep request: ${IMAGE_REQUEST_FILE}"
    fi
    if [ -f "$PREP_GUIDE_FILE" ]; then
        echo "Prep guide: ${PREP_GUIDE_FILE}"
    fi
else
    echo "No project mounted."
fi

echo ""

# ── Login prompts if needed ──────────────────────────────────────────
if [ ${#NEEDS_LOGIN[@]} -gt 0 ]; then
    echo "── First-time setup ──────────────────────"
    for login_cmd in "${NEEDS_LOGIN[@]}"; do
        echo "  Run '$login_cmd' to authenticate"
        echo "  (copy the URL into your host browser)"
    done
    echo ""
    echo "  Auth is saved to Docker volumes and"
    echo "  persists across container restarts."
    echo "───────────────────────────────────────────"
    echo ""
fi

echo "Usage:"
echo "  ${AGENT_COMMAND}         -> Start ${AGENT_NAME}"
if [ "$AGENT_COMMAND" = "claude" ]; then
    echo "  claude-dsp     -> Start with --dangerously-skip-permissions"
fi
echo ""
echo "============================================"
echo ""

# Drop into interactive shell or run provided command
if [ $# -eq 0 ]; then
    exec /bin/bash
else
    exec "$@"
fi
