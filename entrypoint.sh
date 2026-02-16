#!/bin/bash
set -e

echo "============================================"
echo "  cdev — Claude Dev Environment"
echo "============================================"
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
sudo chown -R dev:"$DEV_GROUP" /home/dev/.claude

# ── Seed config defaults into persistent volumes ─────────────────────
# Volume is mounted at ~/.claude. On first run it's
# empty, so we copy baked-in defaults. On subsequent runs the volume
# already has config + credentials, so we only add missing files.
for src in /home/dev/.claude-defaults/*; do
    [ -f "$src" ] || continue
    dest="/home/dev/.claude/$(basename "$src")"
    [ -f "$dest" ] || cp "$src" "$dest"
done

# ── Symlink ~/.claude.json into the persistent volume ────────────────
# Claude Code reads/writes ~/.claude.json (account metadata, onboarding
# state, etc.) which lives OUTSIDE ~/.claude/. We store the real file
# inside the persistent volume and symlink it so writes persist.
rm -f /home/dev/.claude.json
if [ ! -f /home/dev/.claude/.claude.json ]; then
    # First run: create an empty JSON object so Claude can populate it
    echo '{}' > /home/dev/.claude/.claude.json
fi
ln -sf /home/dev/.claude/.claude.json /home/dev/.claude.json

# ── Shell aliases ────────────────────────────────────────────────────
echo "alias claude-dsp='claude --dangerously-skip-permissions'" >> /home/dev/.bashrc

# ── Check tool availability ──────────────────────────────────────────
if command -v claude &>/dev/null; then
    echo "[OK] Claude Code available"
else
    echo "[!!] Claude Code not found"
fi

# ── Check auth status ────────────────────────────────────────────────
NEEDS_LOGIN=()

if [ -f /home/dev/.claude/.credentials.json ]; then
    echo "[OK] Claude authenticated (persistent)"
else
    echo "[--] Claude not logged in"
    NEEDS_LOGIN+=("claude")
fi

echo ""

# ── Project ──────────────────────────────────────────────────────────
if [ -d /home/dev/project ] && [ "$(ls -A /home/dev/project 2>/dev/null)" ]; then
    echo "Project: /home/dev/project"
    cd /home/dev/project
else
    echo "No project mounted."
fi

echo ""

# ── Login prompts if needed ──────────────────────────────────────────
if [ ${#NEEDS_LOGIN[@]} -gt 0 ]; then
    echo "── First-time setup ──────────────────────"
    for tool in "${NEEDS_LOGIN[@]}"; do
        echo "  Run '$tool login' to authenticate"
        echo "  (copy the URL into your host browser)"
    done
    echo ""
    echo "  Auth is saved to Docker volumes and"
    echo "  persists across container restarts."
    echo "───────────────────────────────────────────"
    echo ""
fi

echo "Usage:"
echo "  claude         -> Start Claude Code"
echo "  claude-dsp     -> Start with --dangerously-skip-permissions"
echo ""
echo "============================================"
echo ""

# Drop into interactive shell or run provided command
if [ $# -eq 0 ]; then
    exec /bin/bash
else
    exec "$@"
fi
