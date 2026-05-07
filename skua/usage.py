# SPDX-License-Identifier: BUSL-1.1
"""Local usage trackers for agent CLIs (claude, codex).

Parses local JSONL transcripts directly (no ccusage / network), producing
two windowed totals per agent: a rolling 5-hour block and a rolling 7-day
window. Designed for the dashboard's once-per-minute refresh.
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

# Cache fetched usage values for this many seconds. The dashboard schedules
# a refresh once per minute, so a TTL just under that avoids redundant work.
USAGE_CACHE_TTL_SECONDS = 55.0

# Hard cap on JSONL parsing per fetch so one runaway session can't stall
# the dashboard refresh thread. The 7-day window can touch many files.
_PARSE_BUDGET_SECONDS = 2.5

WINDOW_5H_SECONDS = 5 * 60 * 60
WINDOW_7D_SECONDS = 7 * 24 * 60 * 60

# Default token caps used to render progress bars. These mirror typical
# Claude Max / equivalent plan ceilings; users can override via env vars
# SKUA_USAGE_LIMIT_<AGENT>_<WINDOW>.
_DEFAULT_LIMITS = {
    ("claude", "5h"): 50_000_000,
    ("claude", "7d"): 1_000_000_000,
    ("codex", "5h"): 10_000_000,
    ("codex", "7d"): 200_000_000,
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}


def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None and now - entry[0] < ttl:
            return entry[1]
    value = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def clear_cache() -> None:
    """Drop the in-process usage cache (test hook)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def usage_limit(agent: str, window: str) -> int:
    """Return the configured token cap for a (agent, window) pair."""
    env_key = f"SKUA_USAGE_LIMIT_{agent.upper()}_{window.upper()}"
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_LIMITS.get((agent, window), 10_000_000)


# ── claude (parses ~/.claude/projects) ───────────────────────────────────


def _claude_projects_dir() -> Path:
    home = os.environ.get("CLAUDE_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "projects"
    return Path.home() / ".claude" / "projects"


def claude_usage() -> dict:
    """Return windowed Claude token usage parsed from local transcripts."""
    return _cached("claude", USAGE_CACHE_TTL_SECONDS,
                   lambda: _scan_jsonl(_claude_projects_dir(), _extract_claude_usage, "claude"))


# ── codex (parses ~/.codex/sessions) ─────────────────────────────────────


def _codex_sessions_dir() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def codex_usage() -> dict:
    """Return windowed Codex token usage parsed from local rollouts."""
    return _cached("codex", USAGE_CACHE_TTL_SECONDS,
                   lambda: _scan_jsonl(_codex_sessions_dir(), _extract_codex_usage, "codex"))


def agent_usage_summary() -> dict:
    """Return ``{agent: usage}`` for both supported agents."""
    return {"claude": claude_usage(), "codex": codex_usage()}


# ── core scanner ─────────────────────────────────────────────────────────


def _scan_jsonl(root: Path, extractor, agent_name: str) -> dict:
    """Walk ``root`` for *.jsonl files, extract per-event token usage, and
    bucket totals into the 5h and 7d windows.
    """
    if not root.is_dir():
        return _err(agent_name, f"no usage data ({root} not found)")

    now = time.time()
    cutoff_5h = now - WINDOW_5H_SECONDS
    cutoff_7d = now - WINDOW_7D_SECONDS
    deadline = time.monotonic() + _PARSE_BUDGET_SECONDS

    bucket_5h = {"input": 0, "output": 0, "cached": 0}
    bucket_7d = {"input": 0, "output": 0, "cached": 0}
    files_seen = 0
    files_read = 0
    truncated = False

    try:
        files = list(root.rglob("*.jsonl"))
    except OSError as exc:
        return _err(agent_name, f"scan failed: {exc}")

    # Newest first so the 5h window converges quickly even when truncated.
    files.sort(key=_safe_mtime, reverse=True)

    for path in files:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_7d:
            continue
        files_seen += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                    line = line.strip()
                    if not line or line[0] != "{":
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    extracted = extractor(obj)
                    if extracted is None:
                        continue
                    ts, (inp, out, cached) = extracted
                    if ts is None or ts < cutoff_7d:
                        continue
                    bucket_7d["input"] += inp
                    bucket_7d["output"] += out
                    bucket_7d["cached"] += cached
                    if ts >= cutoff_5h:
                        bucket_5h["input"] += inp
                        bucket_5h["output"] += out
                        bucket_5h["cached"] += cached
        except OSError:
            continue
        files_read += 1

    return _ok(
        agent=agent_name,
        windows={
            "5h": _window_dict(agent_name, "5h", bucket_5h),
            "7d": _window_dict(agent_name, "7d", bucket_7d),
        },
        files_seen=files_seen,
        files_read=files_read,
        truncated=truncated,
    )


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _window_dict(agent: str, window: str, bucket: dict) -> dict:
    inp = int(bucket.get("input", 0))
    out = int(bucket.get("output", 0))
    cached = int(bucket.get("cached", 0))
    total = inp + out + cached
    limit = usage_limit(agent, window)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cached_tokens": cached,
        "total_tokens": total,
        "limit": limit,
        "fraction": min(1.0, total / limit) if limit > 0 else 0.0,
    }


# ── claude extractor ─────────────────────────────────────────────────────


def _extract_claude_usage(obj):
    """Pull (timestamp, (input, output, cached)) from a Claude transcript line."""
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = _i(usage, "input_tokens")
    out = _i(usage, "output_tokens")
    cached = _i(usage, "cache_read_input_tokens") + _i(usage, "cache_creation_input_tokens")
    if inp == 0 and out == 0 and cached == 0:
        return None
    ts = _parse_timestamp(obj.get("timestamp"))
    return ts, (inp, out, cached)


# ── codex extractor ──────────────────────────────────────────────────────


def _extract_codex_usage(obj):
    """Pull (timestamp, (input, output, cached)) from a Codex rollout line."""
    if not isinstance(obj, dict):
        return None
    candidates = [obj]
    for k in ("event", "payload"):
        v = obj.get(k)
        if isinstance(v, dict):
            candidates.append(v)

    for cand in candidates:
        if not isinstance(cand, dict):
            continue

        if cand.get("type") not in ("token_count", "usage", None):
            continue

        info = cand.get("info")
        if isinstance(info, dict):
            usage = info.get("total_token_usage") or info.get("last_token_usage")
            if isinstance(usage, dict):
                triple = _coerce_usage_triple(usage)
                if triple:
                    return _parse_timestamp(obj.get("timestamp")), triple

        usage = cand.get("usage")
        if isinstance(usage, dict):
            triple = _coerce_usage_triple(usage)
            if triple:
                return _parse_timestamp(obj.get("timestamp")), triple

        if cand.get("type") == "token_count":
            triple = _coerce_usage_triple(cand)
            if triple:
                return _parse_timestamp(obj.get("timestamp")), triple

    return None


def _coerce_usage_triple(d: dict):
    inp = _i(d, "input_tokens", "prompt_tokens", "input")
    out = _i(d, "output_tokens", "completion_tokens", "output")
    cached = _i(d, "cached_input_tokens", "cache_read_input_tokens", "cached_tokens")
    if inp == 0 and out == 0 and cached == 0:
        return None
    return inp, out, cached


# ── helpers ──────────────────────────────────────────────────────────────


def _i(d: dict, *keys) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _parse_timestamp(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # ms → s heuristic: anything past year ~3000 is treated as ms
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _ok(*, agent: str, windows: dict, files_seen: int, files_read: int, truncated: bool) -> dict:
    return {
        "ok": True,
        "agent": agent,
        "windows": windows,
        "files_seen": files_seen,
        "files_read": files_read,
        "truncated": truncated,
        "error": "",
    }


def _err(agent: str, message: str) -> dict:
    blank = _window_dict(agent, "5h", {})
    blank7 = _window_dict(agent, "7d", {})
    return {
        "ok": False,
        "agent": agent,
        "windows": {"5h": blank, "7d": blank7},
        "files_seen": 0,
        "files_read": 0,
        "truncated": False,
        "error": message,
    }


# ── formatting helpers ───────────────────────────────────────────────────


def format_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def render_bar(fraction: float, width: int) -> str:
    """Return a unicode progress bar of ``width`` cells for [0, 1]."""
    width = max(1, int(width))
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)
