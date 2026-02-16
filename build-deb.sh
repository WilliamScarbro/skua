#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
Depends: python3, docker.io | docker-ce
Maintainer: William Scarbro
Description: Skua - Dockerized Claude Code Manager
 A CLI tool for building, running, and managing Dockerized Claude Code
 development environments across multiple projects.
EOF

# ── Install files ─────────────────────────────────────────────────────────
cp "$SCRIPT_DIR/skua"          "$PKG_DIR/usr/lib/skua/skua"
cp "$SCRIPT_DIR/Dockerfile"    "$PKG_DIR/usr/lib/skua/Dockerfile"
cp "$SCRIPT_DIR/entrypoint.sh" "$PKG_DIR/usr/lib/skua/entrypoint.sh"
cp "$SCRIPT_DIR/VERSION"       "$PKG_DIR/usr/lib/skua/VERSION"

chmod 755 "$PKG_DIR/usr/lib/skua/skua"
chmod 755 "$PKG_DIR/usr/lib/skua/entrypoint.sh"

# Symlink skua into PATH
ln -sf /usr/lib/skua/skua "$PKG_DIR/usr/bin/skua"

# ── Build package ─────────────────────────────────────────────────────────
dpkg-deb --build "$PKG_DIR"

# Move .deb to repo root for convenience
mv "$PKG_DIR.deb" "$SCRIPT_DIR/${PKG_NAME}_${VERSION}_all.deb"
rm -rf "$BUILD_DIR"

echo ""
echo "Package built: ${PKG_NAME}_${VERSION}_all.deb"
echo ""
echo "Install with:   sudo dpkg -i ${PKG_NAME}_${VERSION}_all.deb"
echo "Uninstall with:  sudo dpkg -r ${PKG_NAME}"
