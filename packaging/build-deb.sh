#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="${1:-$(cat "$SCRIPT_DIR/VERSION")}"
PKG_NAME="skua"
BUILD_DIR="$SCRIPT_DIR/deb-build"
PKG_DIR="$BUILD_DIR/${PKG_NAME}_${VERSION}_all"

echo "Building ${PKG_NAME} ${VERSION} .deb package..."

# Clean previous build
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR/DEBIAN"
mkdir -p "$PKG_DIR/usr/lib/skua"
mkdir -p "$PKG_DIR/usr/bin"

# ── Control file ──────────────────────────────────────────────────────────
cat > "$PKG_DIR/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Architecture: all
Depends: python3, python3-yaml, docker.io | docker-ce
Maintainer: William Scarbro
Description: Skua - Dockerized Claude Code Manager
 A CLI tool for building, running, and managing Dockerized Claude Code
 development environments across multiple projects.
EOF

# ── Install files ─────────────────────────────────────────────────────────
# Python package (includes container/ and presets/)
cp -r "$REPO_ROOT/skua"            "$PKG_DIR/usr/lib/skua/skua"

# Entry point
cp "$REPO_ROOT/bin/skua"           "$PKG_DIR/usr/lib/skua/bin-skua"
chmod 755 "$PKG_DIR/usr/lib/skua/bin-skua"

# Version file
cp "$SCRIPT_DIR/VERSION"           "$PKG_DIR/usr/lib/skua/VERSION"

# Create wrapper script that sets PYTHONPATH
cat > "$PKG_DIR/usr/bin/skua" <<'WRAPPER'
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, "/usr/lib/skua")
from skua.cli import main
main()
WRAPPER
chmod 755 "$PKG_DIR/usr/bin/skua"

# ── Build package ─────────────────────────────────────────────────────────
dpkg-deb --build "$PKG_DIR"

# Move .deb to repo root for convenience
mv "$PKG_DIR.deb" "$REPO_ROOT/${PKG_NAME}_${VERSION}_all.deb"
rm -rf "$BUILD_DIR"

echo ""
echo "Package built: ${PKG_NAME}_${VERSION}_all.deb"
echo ""
echo "Install with:   sudo dpkg -i ${PKG_NAME}_${VERSION}_all.deb"
echo "Uninstall with:  sudo dpkg -r ${PKG_NAME}"
