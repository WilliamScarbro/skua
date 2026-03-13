#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKUA="$SCRIPT_DIR/bin/skua"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

in_virtualenv() {
    python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else 1)
PY
}

ensure_pip() {
    if python3 -m pip --version >/dev/null 2>&1; then
        return
    fi
    python3 -m ensurepip --upgrade >/dev/null 2>&1
}

pip_install_local() {
    ensure_pip
    if in_virtualenv; then
        python3 -m pip install --upgrade "$SCRIPT_DIR"
        return
    fi

    python3 -m pip install --user --break-system-packages --upgrade "$SCRIPT_DIR" 2>/dev/null \
        || python3 -m pip install --user --upgrade "$SCRIPT_DIR"
}

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

# Check/install Python runtime dependencies for skua.
if ! python3 -c "import yaml, rich, textual" 2>/dev/null; then
    echo "Installing Python dependencies from requirements.txt..."
    ensure_pip
    if [ -f "$REQ_FILE" ]; then
        if in_virtualenv; then
            python3 -m pip install -r "$REQ_FILE"
        else
            python3 -m pip install --user --break-system-packages -r "$REQ_FILE" 2>/dev/null \
                || python3 -m pip install --user -r "$REQ_FILE"
        fi
    else
        if in_virtualenv; then
            python3 -m pip install pyyaml rich textual
        else
            python3 -m pip install --user --break-system-packages pyyaml rich textual 2>/dev/null \
                || python3 -m pip install --user pyyaml rich textual
        fi
    fi
fi

# Try to install a clipboard tool for export/copy convenience.
if ! command -v wl-copy >/dev/null 2>&1 && ! command -v xclip >/dev/null 2>&1 && ! command -v xsel >/dev/null 2>&1 && ! command -v pbcopy >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        echo "Installing xclip for clipboard support..."
        if command -v sudo >/dev/null 2>&1; then
            sudo apt-get update -y && sudo apt-get install -y xclip || true
        elif [ "$(id -u)" -eq 0 ]; then
            apt-get update -y && apt-get install -y xclip || true
        fi
    fi
fi

# Verify Docker daemon is running
if ! docker info &>/dev/null; then
    echo "Error: Docker daemon is not running."
    echo "Start Docker and re-run this script."
    exit 1
fi

echo "[OK] Prerequisites: docker, python3, git, pyyaml, rich, textual"
echo ""

# ── Install skua package + CLI ───────────────────────────────────────
echo "Installing skua Python package and CLI..."

pip_install_local

INSTALL_DIR="$HOME/.local/bin"
if in_virtualenv; then
    INSTALL_DIR="$(python3 - <<'PY'
import sys
from pathlib import Path
print(Path(sys.prefix) / "bin")
PY
)"
fi

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$INSTALL_DIR"; then
    echo ""
    echo "  WARNING: $INSTALL_DIR is not in your PATH."
    echo "  Add it by appending this to your shell profile:"
    echo ""
    SHELL_NAME="$(basename "$SHELL")"
    case "$SHELL_NAME" in
        zsh)  PROFILE="~/.zshrc" ;;
        bash) PROFILE="~/.bashrc" ;;
        *)    PROFILE="~/.profile" ;;
    esac
    echo "    echo 'export PATH=\"${INSTALL_DIR/#$HOME/\$HOME}:\$PATH\"' >> $PROFILE"
    echo ""
    echo "  Then restart your shell or run: source $PROFILE"
fi

# Verify skua is callable
if command -v skua &>/dev/null && skua --version &>/dev/null; then
    echo "[OK] Verified: $(skua --version) is available on PATH"
else
    echo ""
    echo "  ERROR: 'skua' is not available on your PATH after install."
    echo "  The console script should be installed at: $INSTALL_DIR/skua"
    echo "  Make sure $INSTALL_DIR is in your PATH, then restart your shell."
    exit 1
fi

echo ""

# ── Run init wizard ──────────────────────────────────────────────────
# The init wizard handles git identity, SSH key, preset installation,
# and global config setup interactively.
"$SKUA" init

echo ""

# ── Done ──────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Installation complete!"
echo "============================================"
echo ""
echo "Quick start:"
echo ""
echo "  skua build"
echo "  skua add <project-name> --dir /path/to/project"
echo "  skua run <project-name>"
echo ""
echo "On first run inside the container:"
echo "  claude /login   (copy the URL into your host browser)"
echo ""
echo "See docs/ for configuration guides and security profiles."
echo ""
