# SPDX-License-Identifier: BUSL-1.1
"""Shared utilities for skua."""

import subprocess
import sys
import shutil
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


def parse_ssh_config_hosts() -> list:
    """Parse ~/.ssh/config and return defined Host names (excludes wildcards)."""
    config_file = Path.home() / ".ssh" / "config"
    if not config_file.is_file():
        return []
    hosts = []
    try:
        for line in config_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("host "):
                parts = stripped.split()[1:]
                for host in parts:
                    if "*" not in host and "?" not in host and "!" not in host:
                        hosts.append(host)
    except OSError:
        return []
    return sorted(set(hosts))


def select_option(prompt: str, options: list, default_index: int = 0) -> str:
    """Select one option, using inline arrow-key UI when running in a TTY."""
    if not options:
        raise ValueError("select_option requires at least one option")

    opts = [str(o) for o in options]
    default_index = max(0, min(default_index, len(opts) - 1))

    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            return _select_option_tty(prompt, opts, default_index)
        except Exception:
            pass

    return _select_option_fallback(prompt, opts, default_index)


def _select_option_tty(prompt: str, options: list, default_index: int) -> str:
    import termios
    import tty

    selected = default_index
    count = len(options)
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    def _render_label(text: str, selected_row: bool) -> str:
        cols = max(20, shutil.get_terminal_size((80, 20)).columns)
        prefix = "> " if selected_row else "  "
        # Keep one spare column to avoid terminal autowrap edge cases.
        max_len = max(1, cols - len(prefix) - 1)
        if len(text) > max_len:
            text = text[:max_len - 1] + "..."
        line = f"{prefix}{text}"
        if selected_row:
            return f"\x1b[7m{line}\x1b[0m"
        return line

    def _draw(redraw: bool):
        if redraw:
            sys.stdout.write(f"\x1b[{count}A")
        for i, option in enumerate(options):
            line = _render_label(option, i == selected)
            sys.stdout.write(f"\r\x1b[2K{line}\n")
        sys.stdout.flush()

    # Keep a blank separator before each interactive selector header.
    print()
    print(prompt)
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()

    try:
        tty.setraw(fd)
        _draw(redraw=False)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                return options[selected]
            if ch == "\x03":
                raise KeyboardInterrupt

            changed = False
            if ch in ("k", "K"):
                selected = (selected - 1) % count
                changed = True
            elif ch in ("j", "J"):
                selected = (selected + 1) % count
                changed = True
            elif ch == "\x1b":
                seq1 = sys.stdin.read(1)
                if seq1 == "[":
                    seq2 = sys.stdin.read(1)
                    if seq2 == "A":
                        selected = (selected - 1) % count
                        changed = True
                    elif seq2 == "B":
                        selected = (selected + 1) % count
                        changed = True
            if changed:
                _draw(redraw=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        # Ensure the next prompt starts at column 1 even after raw-mode redraws.
        sys.stdout.write("\x1b[?25h\r\x1b[2K")
        sys.stdout.flush()


def _select_option_fallback(prompt: str, options: list, default_index: int) -> str:
    default = options[default_index]
    print()
    print(prompt)
    for i, option in enumerate(options, start=1):
        suffix = " (default)" if i - 1 == default_index else ""
        print(f"  {i}. {option}{suffix}")

    while True:
        raw = input(f"Choose option [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        if raw in options:
            return raw
        print("Invalid selection. Enter a number from the list.")
