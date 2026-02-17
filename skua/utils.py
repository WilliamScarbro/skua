# SPDX-License-Identifier: BUSL-1.1
"""Shared utilities for skua."""

import subprocess
import sys
from pathlib import Path


def detect_git_identity() -> tuple:
    """Auto-detect git name/email from global git config.

    Returns (name, email) tuple, either may be empty string.
    """
    name = ""
    email = ""
    try:
        name = subprocess.check_output(
            ["git", "config", "--global", "user.name"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        email = subprocess.check_output(
            ["git", "config", "--global", "user.email"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return name, email


def die(msg: str, code: int = 1):
    """Print error message and exit."""
    print(f"Error: {msg}")
    sys.exit(code)


def confirm(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question. Returns True for yes."""
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def find_ssh_keys() -> list:
    """List available SSH private keys in ~/.ssh/."""
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.is_dir():
        return []
    skip = {"known_hosts", "config", "authorized_keys"}
    return sorted(
        f for f in ssh_dir.iterdir()
        if f.is_file() and not f.name.endswith(".pub") and f.name not in skip
    )
