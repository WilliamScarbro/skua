#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
set -euo pipefail

say() { printf '%s\n' "$*"; }
err() { printf 'Error: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

if [[ "${OSTYPE:-}" != linux* ]] && [[ "$(uname -s)" != "Linux" ]]; then
    err "This installer currently supports Linux only."
    exit 1
fi

arch_raw="$(uname -m)"
case "$arch_raw" in
    x86_64|amd64) docker_arch="x86_64" ;;
    aarch64|arm64) docker_arch="aarch64" ;;
    armv7l|armhf) docker_arch="armhf" ;;
    ppc64le) docker_arch="ppc64le" ;;
    s390x) docker_arch="s390x" ;;
    *)
        err "Unsupported architecture: $arch_raw"
        exit 1
        ;;
esac

find_pkg_mgr() {
    for pm in apt-get dnf yum zypper pacman apk; do
        if have "$pm"; then
            printf '%s\n' "$pm"
            return 0
        fi
    done
    return 1
}

install_download_tool() {
    if have curl || have wget; then
        return 0
    fi

    pm="$(find_pkg_mgr || true)"
    if [[ -z "$pm" ]]; then
        err "Neither curl nor wget is installed, and no supported package manager was detected."
        return 1
    fi

    say "Installing curl via $pm..."
    case "$pm" in
        apt-get)
            sudo apt-get update -y
            sudo apt-get install -y curl
            ;;
        dnf)
            sudo dnf install -y curl
            ;;
        yum)
            sudo yum install -y curl
            ;;
        zypper)
            sudo zypper --non-interactive install curl
            ;;
        pacman)
            sudo pacman -Sy --noconfirm curl
            ;;
        apk)
            sudo apk add --no-cache curl
            ;;
        *)
            err "Unsupported package manager: $pm"
            return 1
            ;;
    esac
}

fetch_to_file() {
    local url="$1"
    local out="$2"
    if have curl; then
        curl -fsSL "$url" -o "$out"
    elif have wget; then
        wget -qO "$out" "$url"
    else
        return 1
    fi
}

say "=== Installing standalone Docker CLI (non-Snap) ==="
say "This installs only the docker CLI binary and does not modify Snap Docker."

if ! install_download_tool; then
    exit 1
fi

tmp="$(mktemp -d)"
cleanup() {
    rm -rf "$tmp"
}
trap cleanup EXIT

base_url="https://download.docker.com/linux/static/stable/${docker_arch}/"
index_html="$tmp/index.html"

say "Discovering latest Docker CLI for architecture: $docker_arch"
fetch_to_file "$base_url" "$index_html"

archive_name="$(grep -Eo 'docker-[0-9]+\.[0-9]+\.[0-9]+\.tgz' "$index_html" | sort -Vu | tail -1 || true)"
if [[ -z "$archive_name" ]]; then
    err "Could not determine latest docker static bundle from $base_url"
    exit 1
fi

archive_url="${base_url}${archive_name}"
archive_path="$tmp/docker.tgz"
say "Downloading ${archive_name}..."
fetch_to_file "$archive_url" "$archive_path"

tar -xzf "$archive_path" -C "$tmp"
if [[ ! -x "$tmp/docker/docker" ]]; then
    err "Downloaded archive did not contain docker CLI binary."
    exit 1
fi

target_dir="${SKUA_DOCKER_CLI_INSTALL_DIR:-}"
if [[ -z "$target_dir" ]]; then
    if [[ -w "/usr/local/bin" ]]; then
        target_dir="/usr/local/bin"
    elif have sudo; then
        target_dir="/usr/local/bin"
    else
        target_dir="$HOME/.local/bin"
    fi
fi

mkdir_cmd=(install -m 0755 "$tmp/docker/docker" "$target_dir/docker")
if [[ "$target_dir" == "/usr/local/bin" && ! -w "/usr/local/bin" ]]; then
    say "Installing docker CLI to /usr/local/bin (sudo required)..."
    sudo mkdir -p "$target_dir"
    sudo "${mkdir_cmd[@]}"
else
    say "Installing docker CLI to $target_dir..."
    mkdir -p "$target_dir"
    "${mkdir_cmd[@]}"
fi

installed_path="$target_dir/docker"
say "Installed Docker CLI: $installed_path"
say "docker version: $("$installed_path" --version)"

if have docker; then
    current="$(command -v docker || true)"
    say "Current 'docker' on PATH: ${current:-not found}"
fi

if [[ ":$PATH:" != *":$target_dir:"* ]]; then
    say
    say "Note: $target_dir is not currently on PATH."
    say "Add it for future shells:"
    say "  export PATH=\"$target_dir:\$PATH\""
fi

say
say "Done. Snap Docker configuration was not modified."
