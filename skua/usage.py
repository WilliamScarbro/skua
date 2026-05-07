# SPDX-License-Identifier: BUSL-1.1
"""Local usage trackers for agent CLIs (claude, codex)."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

# Cache fetched usage values for this many seconds. The dashboard schedules
# a refresh once per minute, so a TTL just under that avoids redundant work
# when several refresh paths fire close together.
USAGE_CACHE_TTL_SECONDS = 55.0

# Hard caps so a misbehaving CLI cannot stall the dashboard refresh thread.
_CCUSAGE_TIMEOUT_SECONDS = 8.0
_CODEX_PARSE_BUDGET_SECONDS = 1.5

# Codex doesn't expose a billing-block window; we approximate by aggregating
# the most recent five hours of rollouts, matching ccusage's claude window.
_CODEX_WINDOW_SECONDS = 5 * 60 * 60

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


def claude_usage() -> dict:
    """Return current Claude billing-block usage via ccusage, or an error stub.

    Result shape:
      {
        "ok": bool,
        "tokens": int,            # total tokens in the active 5h block
        "cost_usd": float,
        "burn_rate_tpm": float,   # tokens/minute
        "remaining_minutes": int, # minutes left in the active block
        "projection_tokens": int,
        "projection_cost_usd": float,
        "error": str,             # populated when ok is False
      }
    """
    return _cached("claude", USAGE_CACHE_TTL_SECONDS, _fetch_claude_usage)


def codex_usage() -> dict:
    """Return Codex token usage aggregated from local rollouts.

    Result shape mirrors ``claude_usage`` but only fields derivable from the
    local JSONL are populated; ``cost_usd`` is reported when codex emits one.
    """
    return _cached("codex", USAGE_CACHE_TTL_SECONDS, _fetch_codex_usage)


def agent_usage_summary() -> dict:
    """Convenience: return a {agent_name: usage_dict} mapping for the dashboard."""
    return {"claude": claude_usage(), "codex": codex_usage()}


# ── claude (ccusage) ─────────────────────────────────────────────────────


def _fetch_claude_usage() -> dict:
    cmd = ["npx", "-y", "--quiet", "ccusage", "blocks", "--active", "--json", "-O"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CCUSAGE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _err("npx not installed; install Node.js to enable claude usage")
    except subprocess.TimeoutExpired:
        return _err("ccusage timed out")

    if proc.returncode != 0:
        first_line = (proc.stderr or proc.stdout or "").strip().splitlines()[:1]
        detail = first_line[0] if first_line else f"exit {proc.returncode}"
        return _err(f"ccusage failed: {detail}")

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return _err("ccusage output not parseable")

    blocks = data.get("blocks") or []
    if not blocks:
        return _ok(tokens=0, cost_usd=0.0, no_active=True)

    block = blocks[0]
    burn = block.get("burnRate") or {}
    proj = block.get("projection") or {}
    return _ok(
        tokens=int(block.get("totalTokens") or 0),
        cost_usd=float(block.get("costUSD") or 0.0),
        burn_rate_tpm=float(burn.get("tokensPerMinute") or 0.0),
        remaining_minutes=int(proj.get("remainingMinutes") or 0),
        projection_tokens=int(proj.get("totalTokens") or 0),
        projection_cost_usd=float(proj.get("totalCost") or 0.0),
        models=list(block.get("models") or []),
    )


# ── codex (local rollouts) ───────────────────────────────────────────────


def _codex_sessions_dir() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def _fetch_codex_usage() -> dict:
    sessions = _codex_sessions_dir()
    if not sessions.is_dir():
        return _err("codex not installed (no ~/.codex/sessions)")

    cutoff = time.time() - _CODEX_WINDOW_SECONDS
    deadline = time.monotonic() + _CODEX_PARSE_BUDGET_SECONDS

    input_t = 0
    output_t = 0
    cached_t = 0
    files_seen = 0
    files_read = 0

    for path in sessions.rglob("*.jsonl"):
        if time.monotonic() > deadline:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        files_seen += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if time.monotonic() > deadline:
                        break
                    line = line.strip()
                    if not line or line[0] != "{":
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    delta = _extract_codex_tokens(obj)
                    if delta is None:
                        continue
                    input_t += delta[0]
                    output_t += delta[1]
                    cached_t += delta[2]
        except OSError:
            continue
        files_read += 1

    total = input_t + output_t
    if files_seen == 0:
        return _ok(tokens=0, cost_usd=0.0, no_active=True)

    return _ok(
        tokens=total,
        input_tokens=input_t,
        output_tokens=output_t,
        cached_tokens=cached_t,
        files=files_read,
        window_hours=_CODEX_WINDOW_SECONDS // 3600,
    )


def _extract_codex_tokens(obj) -> tuple[int, int, int] | None:
    """Pull (input, output, cached) token counts from a codex rollout entry.

    Codex's rollout JSONL has evolved, so this tries several known shapes:
      - {"type": "token_count", "info": {"total_token_usage": {...}}}
      - {"type": "token_count", "input_tokens": ..., "output_tokens": ...}
      - {"event": {"type": "token_count", ...}}
      - {"usage": {"input_tokens": ..., "output_tokens": ...}}
    """
    if not isinstance(obj, dict):
        return None

    candidates = [obj]
    inner = obj.get("event")
    if isinstance(inner, dict):
        candidates.append(inner)
    payload = obj.get("payload")
    if isinstance(payload, dict):
        candidates.append(payload)

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        if cand.get("type") not in ("token_count", "usage", None):
            continue

        info = cand.get("info")
        if isinstance(info, dict):
            usage = info.get("total_token_usage") or info.get("last_token_usage")
            if isinstance(usage, dict):
                return _coerce_usage_triple(usage)

        usage = cand.get("usage")
        if isinstance(usage, dict):
            return _coerce_usage_triple(usage)

        if cand.get("type") == "token_count":
            return _coerce_usage_triple(cand)

    return None


def _coerce_usage_triple(usage: dict) -> tuple[int, int, int] | None:
    def _i(*keys):
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    inp = _i("input_tokens", "prompt_tokens", "input")
    out = _i("output_tokens", "completion_tokens", "output")
    cached = _i("cached_input_tokens", "cache_read_input_tokens", "cached_tokens")
    if inp == 0 and out == 0 and cached == 0:
        return None
    return inp, out, cached


# ── helpers ──────────────────────────────────────────────────────────────


def _ok(**fields) -> dict:
    base = {
        "ok": True,
        "tokens": 0,
        "cost_usd": 0.0,
        "burn_rate_tpm": 0.0,
        "remaining_minutes": 0,
        "projection_tokens": 0,
        "projection_cost_usd": 0.0,
        "error": "",
    }
    base.update(fields)
    return base


def _err(message: str) -> dict:
    return {
        "ok": False,
        "tokens": 0,
        "cost_usd": 0.0,
        "burn_rate_tpm": 0.0,
        "remaining_minutes": 0,
        "projection_tokens": 0,
        "projection_cost_usd": 0.0,
        "error": message,
    }


# ── formatting helpers (used by the dashboard) ───────────────────────────


def format_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_cost(usd: float) -> str:
    return f"${float(usd or 0.0):,.2f}"


def format_remaining(minutes: int) -> str:
    minutes = int(minutes or 0)
    if minutes <= 0:
        return "—"
    h, m = divmod(minutes, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def format_burn_rate(tpm: float) -> str:
    tpm = float(tpm or 0.0)
    if tpm <= 0:
        return "—"
    if tpm >= 1000:
        return f"{tpm / 1000:.1f}k/min"
    return f"{tpm:.0f}/min"
