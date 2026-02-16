#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKUA="$SCRIPT_DIR/skua"

echo "============================================"
echo "  skua installer"
echo "  Dockerized Claude Code Manager"
echo "============================================"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────
missing=""
command -v docker &>/dev/null || missing="$missing docker"
command -v python3 &>/dev/null || missing="$missing python3"
command -v git &>/dev/null || missing="$missing git"

if [ -n "$missing" ]; then
    echo "Error: missing required tools:$missing"
    echo "Install them and re-run this script."
    exit 1
fi

# Verify Docker daemon is running
if ! docker info &>/dev/null; then
    echo "Error: Docker daemon is not running."
    echo "Start Docker and re-run this script."
    exit 1
fi

echo "[OK] Prerequisites: docker, python3, git"
echo ""

# ── Git identity ──────────────────────────────────────────────────────
DEFAULT_GIT_NAME=$(git config --global user.name 2>/dev/null || echo "")
DEFAULT_GIT_EMAIL=$(git config --global user.email 2>/dev/null || echo "")

read -rp "Git user name [$DEFAULT_GIT_NAME]: " GIT_NAME
GIT_NAME="${GIT_NAME:-$DEFAULT_GIT_NAME}"

read -rp "Git user email [$DEFAULT_GIT_EMAIL]: " GIT_EMAIL
GIT_EMAIL="${GIT_EMAIL:-$DEFAULT_GIT_EMAIL}"

if [ -z "$GIT_NAME" ] || [ -z "$GIT_EMAIL" ]; then
    echo "Error: Git name and email are required."
    exit 1
fi

echo ""

# ── SSH key (optional) ────────────────────────────────────────────────
SSH_KEY=""
if ls "$HOME/.ssh"/*.pub &>/dev/null; then
    echo "Available SSH keys:"
    for pub in "$HOME/.ssh"/*.pub; do
        echo "  ${pub%.pub}"
    done
    echo ""
fi
read -rp "SSH private key for git operations (leave empty to skip): " SSH_KEY

if [ -n "$SSH_KEY" ]; then
    SSH_KEY="$(realpath "$SSH_KEY" 2>/dev/null || echo "$SSH_KEY")"
    if [ ! -f "$SSH_KEY" ]; then
        echo "Warning: $SSH_KEY not found, skipping."
        SSH_KEY=""
    fi
fi

echo ""

# ── Install skua to PATH ─────────────────────────────────────────────
echo "Installing skua CLI..."

chmod +x "$SKUA"

# Find a suitable bin directory that's already in PATH
INSTALL_DIR=""
for candidate in "$HOME/.local/bin" "$HOME/bin" "/usr/local/bin"; do
    if echo "$PATH" | tr ':' '\n' | grep -qx "$candidate"; then
        if [ -w "$candidate" ] || [ ! -e "$candidate" -a -w "$(dirname "$candidate")" ]; then
            INSTALL_DIR="$candidate"
            break
        fi
    fi
done

if [ -n "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
    ln -sf "$SKUA" "$INSTALL_DIR/skua"
    echo "[OK] Symlinked skua -> $INSTALL_DIR/skua"
else
    # None of the standard dirs are in PATH — use ~/.local/bin and warn
    INSTALL_DIR="$HOME/.local/bin"
    mkdir -p "$INSTALL_DIR"
    ln -sf "$SKUA" "$INSTALL_DIR/skua"
    echo "[!!] Symlinked skua -> $INSTALL_DIR/skua"
    echo ""
    echo "  WARNING: $INSTALL_DIR is not in your PATH."
    echo "  Add it by appending this to your shell profile:"
    echo ""
    # Detect shell
    SHELL_NAME="$(basename "$SHELL")"
    case "$SHELL_NAME" in
        zsh)  PROFILE="~/.zshrc" ;;
        bash) PROFILE="~/.bashrc" ;;
        *)    PROFILE="~/.profile" ;;
    esac
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $PROFILE"
    echo ""
    echo "  Then restart your shell or run: source $PROFILE"
fi

echo ""

# ── Configure skua ────────────────────────────────────────────────────
echo "Saving configuration..."

CONFIG_ARGS="--git-name \"$GIT_NAME\" --git-email \"$GIT_EMAIL\" --tool-dir \"$SCRIPT_DIR\""
if [ -n "$SSH_KEY" ]; then
    CONFIG_ARGS="$CONFIG_ARGS --ssh-key \"$SSH_KEY\""
fi
eval python3 "$SKUA" config $CONFIG_ARGS
echo ""

# ── Build the Docker image ────────────────────────────────────────────
echo "Building Docker image (this may take a few minutes)..."
echo ""
python3 "$SKUA" build

echo ""

# ── Done ──────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Installation complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo ""
if [ -n "$SSH_KEY" ]; then
    echo "  skua add <project-name> --dir /path/to/project --ssh-key $SSH_KEY"
else
    echo "  skua add <project-name> --dir /path/to/project"
fi
echo "  skua run <project-name>"
echo ""
echo "On first run inside the container:"
echo "  claude login    (copy the URL into your host browser)"
echo ""
