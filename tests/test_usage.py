# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua.usage agent usage helpers."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua import usage


def _ccusage_payload(**overrides) -> str:
    block = {
        "id": "2026-05-07T16:00:00.000Z",
        "isActive": True,
        "totalTokens": 8_790_260,
        "costUSD": 6.9238,
        "burnRate": {"tokensPerMinute": 35987.7},
        "projection": {"totalTokens": 10_640_129, "totalCost": 8.38, "remainingMinutes": 51},
        "models": ["claude-opus-4-7"],
    }
    block.update(overrides)
    return json.dumps({"blocks": [block]})


class TestFormatters(unittest.TestCase):
    def test_format_tokens_handles_scales(self):
        self.assertEqual(usage.format_tokens(0), "0")
        self.assertEqual(usage.format_tokens(950), "950")
        self.assertEqual(usage.format_tokens(1500), "1.5k")
        self.assertEqual(usage.format_tokens(8_790_260), "8.79M")

    def test_format_cost_pads_to_two_decimals(self):
        self.assertEqual(usage.format_cost(0), "$0.00")
        self.assertEqual(usage.format_cost(6.9238), "$6.92")
        self.assertEqual(usage.format_cost(1234.5), "$1,234.50")

    def test_format_remaining_handles_zero_and_hours(self):
        self.assertEqual(usage.format_remaining(0), "—")
        self.assertEqual(usage.format_remaining(45), "45m")
        self.assertEqual(usage.format_remaining(125), "2h05m")

    def test_format_burn_rate(self):
        self.assertEqual(usage.format_burn_rate(0), "—")
        self.assertEqual(usage.format_burn_rate(750), "750/min")
        self.assertEqual(usage.format_burn_rate(35987.7), "36.0k/min")


class TestClaudeUsage(unittest.TestCase):
    def setUp(self):
        usage.clear_cache()

    def tearDown(self):
        usage.clear_cache()

    def test_parses_active_block(self):
        proc = mock.Mock(returncode=0, stdout=_ccusage_payload(), stderr="")
        with mock.patch("skua.usage.subprocess.run", return_value=proc):
            result = usage.claude_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 8_790_260)
        self.assertAlmostEqual(result["cost_usd"], 6.9238)
        self.assertEqual(result["remaining_minutes"], 51)
        self.assertAlmostEqual(result["burn_rate_tpm"], 35987.7)
        self.assertEqual(result["models"], ["claude-opus-4-7"])

    def test_no_active_block(self):
        proc = mock.Mock(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")
        with mock.patch("skua.usage.subprocess.run", return_value=proc):
            result = usage.claude_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 0)
        self.assertTrue(result.get("no_active"))

    def test_missing_npx_returns_error_stub(self):
        with mock.patch("skua.usage.subprocess.run", side_effect=FileNotFoundError):
            result = usage.claude_usage()
        self.assertFalse(result["ok"])
        self.assertIn("npx", result["error"].lower())

    def test_ccusage_timeout(self):
        with mock.patch(
            "skua.usage.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="npx", timeout=1),
        ):
            result = usage.claude_usage()
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])

    def test_garbage_output_handled(self):
        proc = mock.Mock(returncode=0, stdout="not json", stderr="")
        with mock.patch("skua.usage.subprocess.run", return_value=proc):
            result = usage.claude_usage()
        self.assertFalse(result["ok"])
        self.assertIn("parseable", result["error"])

    def test_results_are_cached(self):
        proc = mock.Mock(returncode=0, stdout=_ccusage_payload(), stderr="")
        with mock.patch("skua.usage.subprocess.run", return_value=proc) as run:
            usage.claude_usage()
            usage.claude_usage()
            usage.claude_usage()
        self.assertEqual(run.call_count, 1)


class TestCodexUsage(unittest.TestCase):
    def setUp(self):
        usage.clear_cache()

    def tearDown(self):
        usage.clear_cache()

    def _write_rollout(self, dir_path: Path, lines: list[str]) -> Path:
        path = dir_path / "rollout-2026-05-07.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_returns_error_when_codex_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("skua.usage._codex_sessions_dir", return_value=Path(tmp) / "absent"):
                result = usage.codex_usage()
        self.assertFalse(result["ok"])
        self.assertIn("codex", result["error"].lower())

    def test_aggregates_token_count_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            self._write_rollout(
                sessions,
                [
                    json.dumps({
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1000,
                                "output_tokens": 500,
                                "cached_input_tokens": 200,
                            },
                        },
                    }),
                    json.dumps({"type": "ignore_me"}),
                    json.dumps({
                        "event": {
                            "type": "token_count",
                            "usage": {"input_tokens": 50, "output_tokens": 25},
                        }
                    }),
                ],
            )
            with mock.patch("skua.usage._codex_sessions_dir", return_value=sessions):
                result = usage.codex_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 1000 + 500 + 50 + 25)
        self.assertEqual(result["input_tokens"], 1050)
        self.assertEqual(result["output_tokens"], 525)
        self.assertEqual(result["cached_tokens"], 200)
        self.assertEqual(result["window_hours"], 5)

    def test_skips_files_outside_window(self):
        import os
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            stale = self._write_rollout(
                sessions,
                [
                    json.dumps({
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 999, "output_tokens": 999}},
                    }),
                ],
            )
            old = _time.time() - (24 * 3600)
            os.utime(stale, (old, old))
            with mock.patch("skua.usage._codex_sessions_dir", return_value=sessions):
                result = usage.codex_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 0)
        self.assertTrue(result.get("no_active"))

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            self._write_rollout(
                sessions,
                [
                    "not json",
                    "",
                    json.dumps({
                        "type": "token_count",
                        "input_tokens": 7,
                        "output_tokens": 3,
                    }),
                ],
            )
            with mock.patch("skua.usage._codex_sessions_dir", return_value=sessions):
                result = usage.codex_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 10)


class TestAgentUsageSummary(unittest.TestCase):
    def setUp(self):
        usage.clear_cache()

    def tearDown(self):
        usage.clear_cache()

    def test_summary_returns_both_agents(self):
        proc = mock.Mock(returncode=0, stdout=_ccusage_payload(), stderr="")
        with mock.patch("skua.usage.subprocess.run", return_value=proc), \
             mock.patch("skua.usage._codex_sessions_dir", return_value=Path("/no/such/path")):
            data = usage.agent_usage_summary()
        self.assertIn("claude", data)
        self.assertIn("codex", data)
        self.assertTrue(data["claude"]["ok"])
        self.assertFalse(data["codex"]["ok"])


if __name__ == "__main__":
    unittest.main()
