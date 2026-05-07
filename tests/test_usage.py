# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua.usage agent usage helpers."""

import json
import os
import sys
import tempfile
import time as _time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua import usage


def _claude_assistant_line(*, ts: str, input_t=10, output_t=5, cache_create=2, cache_read=3):
    return json.dumps({
        "timestamp": ts,
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            },
        },
    })


def _codex_token_count_line(*, ts, input_t=10, output_t=5, cached=0):
    return json.dumps({
        "timestamp": ts,
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cached_input_tokens": cached,
            },
        },
    })


def _iso(ts_seconds: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TestFormatters(unittest.TestCase):
    def test_format_tokens_handles_scales(self):
        self.assertEqual(usage.format_tokens(0), "0")
        self.assertEqual(usage.format_tokens(950), "950")
        self.assertEqual(usage.format_tokens(1500), "1.5k")
        self.assertEqual(usage.format_tokens(8_790_260), "8.79M")

    def test_render_bar_proportional(self):
        self.assertEqual(usage.render_bar(0, 10), "░" * 10)
        self.assertEqual(usage.render_bar(1, 10), "█" * 10)
        self.assertEqual(usage.render_bar(0.5, 10), "█" * 5 + "░" * 5)
        # Out of range clamps.
        self.assertEqual(usage.render_bar(1.5, 4), "█" * 4)
        self.assertEqual(usage.render_bar(-0.5, 4), "░" * 4)


class TestClaudeUsage(unittest.TestCase):
    def setUp(self):
        usage.clear_cache()

    def tearDown(self):
        usage.clear_cache()

    def test_returns_error_when_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "missing"
            with mock.patch("skua.usage._claude_projects_dir", return_value=absent):
                result = usage.claude_usage()
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])

    def test_buckets_into_5h_and_7d(self):
        now = _time.time()
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects" / "p1"
            projects.mkdir(parents=True)
            jsonl = projects / "session.jsonl"
            lines = [
                _claude_assistant_line(ts=_iso(now - 60), input_t=100, output_t=50,
                                       cache_create=20, cache_read=30),
                _claude_assistant_line(ts=_iso(now - 4 * 3600), input_t=200, output_t=80,
                                       cache_create=0, cache_read=0),
                _claude_assistant_line(ts=_iso(now - 6 * 3600), input_t=400, output_t=160,
                                       cache_create=0, cache_read=0),
                _claude_assistant_line(ts=_iso(now - 9 * 24 * 3600), input_t=99999,
                                       output_t=99999, cache_create=0, cache_read=0),
            ]
            jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with mock.patch("skua.usage._claude_projects_dir", return_value=projects.parent):
                result = usage.claude_usage()
        self.assertTrue(result["ok"], result)
        w5 = result["windows"]["5h"]
        w7 = result["windows"]["7d"]
        # 5h: only the first two events qualify (within 5h)
        self.assertEqual(w5["input_tokens"], 100 + 200)
        self.assertEqual(w5["output_tokens"], 50 + 80)
        self.assertEqual(w5["cached_tokens"], 20 + 30)
        # 7d: includes the 6h-old one too, excludes the 9-day-old one
        self.assertEqual(w7["input_tokens"], 100 + 200 + 400)
        self.assertEqual(w7["output_tokens"], 50 + 80 + 160)
        self.assertEqual(w7["cached_tokens"], 20 + 30)
        # fraction is bounded
        self.assertGreaterEqual(w5["fraction"], 0.0)
        self.assertLessEqual(w5["fraction"], 1.0)

    def test_skips_lines_without_usage_or_timestamp(self):
        now = _time.time()
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects" / "p"
            projects.mkdir(parents=True)
            jsonl = projects / "session.jsonl"
            jsonl.write_text(
                "\n".join([
                    json.dumps({"type": "user", "timestamp": _iso(now)}),
                    "not json",
                    _claude_assistant_line(ts=_iso(now), input_t=7, output_t=3,
                                           cache_create=0, cache_read=0),
                    json.dumps({"message": {"usage": {"input_tokens": 1}}}),  # no timestamp
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch("skua.usage._claude_projects_dir", return_value=projects.parent):
                result = usage.claude_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["windows"]["5h"]["total_tokens"], 10)

    def test_results_are_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            projects.mkdir()
            with mock.patch("skua.usage._claude_projects_dir", return_value=projects) as patched:
                usage.claude_usage()
                usage.claude_usage()
                usage.claude_usage()
            self.assertEqual(patched.call_count, 1)

    def test_env_override_changes_limit(self):
        with mock.patch.dict(os.environ, {"SKUA_USAGE_LIMIT_CLAUDE_5H": "1000"}):
            self.assertEqual(usage.usage_limit("claude", "5h"), 1000)


class TestCodexUsage(unittest.TestCase):
    def setUp(self):
        usage.clear_cache()

    def tearDown(self):
        usage.clear_cache()

    def test_returns_error_when_codex_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent"
            with mock.patch("skua.usage._codex_sessions_dir", return_value=absent):
                result = usage.codex_usage()
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])

    def test_aggregates_token_count_events(self):
        now = _time.time()
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            (sessions / "rollout.jsonl").write_text(
                "\n".join([
                    _codex_token_count_line(ts=_iso(now - 30), input_t=1000,
                                            output_t=500, cached=200),
                    _codex_token_count_line(ts=_iso(now - 6 * 3600), input_t=300,
                                            output_t=100, cached=0),
                    _codex_token_count_line(ts=_iso(now - 8 * 24 * 3600), input_t=99,
                                            output_t=99, cached=0),
                ]) + "\n",
                encoding="utf-8",
            )
            with mock.patch("skua.usage._codex_sessions_dir", return_value=sessions):
                result = usage.codex_usage()
        self.assertTrue(result["ok"])
        w5 = result["windows"]["5h"]
        w7 = result["windows"]["7d"]
        self.assertEqual(w5["input_tokens"], 1000)
        self.assertEqual(w5["output_tokens"], 500)
        self.assertEqual(w5["cached_tokens"], 200)
        self.assertEqual(w7["input_tokens"], 1000 + 300)
        self.assertEqual(w7["output_tokens"], 500 + 100)


class TestAgentUsageSummary(unittest.TestCase):
    def setUp(self):
        usage.clear_cache()

    def tearDown(self):
        usage.clear_cache()

    def test_summary_returns_both_agents(self):
        with mock.patch("skua.usage._claude_projects_dir", return_value=Path("/no/such")), \
             mock.patch("skua.usage._codex_sessions_dir", return_value=Path("/no/such")):
            data = usage.agent_usage_summary()
        self.assertIn("claude", data)
        self.assertIn("codex", data)
        self.assertFalse(data["claude"]["ok"])
        self.assertFalse(data["codex"]["ok"])
        # Even on error, window stubs are present so the renderer works.
        for agent in ("claude", "codex"):
            self.assertIn("5h", data[agent]["windows"])
            self.assertIn("7d", data[agent]["windows"])


if __name__ == "__main__":
    unittest.main()
