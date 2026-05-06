#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
"""Tests for `skua stop` git safety checks."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.resources import Project


class TestStopGitChecks(unittest.TestCase):
    def test_directory_git_repo_prompts_even_without_repo_url(self):
        from skua.commands import stop as stop_cmd

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / ".git").mkdir()
            project = Project(name="demo", directory=str(repo_dir), repo="", host="")
            store = mock.Mock()
            store.repo_dir.return_value = Path("/unused")

            with mock.patch.object(stop_cmd, "_git_status", return_value="UNCLEAN") as mock_git_status:
                with mock.patch.object(stop_cmd, "confirm", return_value=False) as mock_confirm:
                    should_continue = stop_cmd._should_continue_for_git(project, store, force=False)

        self.assertFalse(should_continue)
        mock_git_status.assert_called_once_with(repo_dir)
        mock_confirm.assert_called_once_with("Stop container anyway?", default=False)


class TestStopCommand(unittest.TestCase):
    @mock.patch("skua.commands.stop.subprocess.run")
    @mock.patch("skua.commands.stop.get_running_skua_containers", return_value=["skua-demo"])
    @mock.patch("skua.commands.stop.ConfigStore")
    def test_cmd_stop_uses_grace_period_for_local_container(self, mock_store_cls, _mock_running, mock_run):
        from skua.commands.stop import cmd_stop

        store = mock.Mock()
        store.resolve_project.return_value = Project(name="demo", directory="", repo="", host="")
        mock_store_cls.return_value = store
        mock_run.return_value = mock.Mock(returncode=0)

        result = cmd_stop(SimpleNamespace(name="demo", force=True), lock_project=False)

        self.assertTrue(result)
        from skua.commands.stop import DOCKER_STOP_TIMEOUT
        mock_run.assert_called_once_with(
            ["docker", "stop", "--time", str(DOCKER_STOP_TIMEOUT), "skua-demo"]
        )

    @mock.patch("skua.commands.stop.subprocess.run")
    @mock.patch("skua.commands.stop.get_running_skua_containers", return_value=["skua-demo"])
    @mock.patch("skua.commands.stop.ConfigStore")
    def test_cmd_stop_uses_grace_period_for_remote_container(self, mock_store_cls, _mock_running, mock_run):
        from skua.commands.stop import cmd_stop

        store = mock.Mock()
        store.resolve_project.return_value = Project(name="demo", directory="", repo="", host="docker.example.com")
        mock_store_cls.return_value = store
        mock_run.return_value = mock.Mock(returncode=0)

        result = cmd_stop(SimpleNamespace(name="demo", force=True), lock_project=False)

        self.assertTrue(result)
        from skua.commands.stop import DOCKER_STOP_TIMEOUT
        mock_run.assert_called_once_with(
            ["ssh", "docker.example.com", "docker", "stop", "--time", str(DOCKER_STOP_TIMEOUT), "skua-demo"]
        )


class TestEntrypointPersistenceHooks(unittest.TestCase):
    def test_entrypoint_flushes_history_on_shell_exit(self):
        entrypoint = Path(__file__).resolve().parent.parent / "skua" / "container" / "entrypoint.sh"
        text = entrypoint.read_text()

        self.assertIn("trap 'history -a' EXIT", text)

    def test_entrypoint_handles_graceful_tmux_shutdown(self):
        entrypoint = Path(__file__).resolve().parent.parent / "skua" / "container" / "entrypoint.sh"
        text = entrypoint.read_text()

        self.assertIn("graceful_tmux_shutdown()", text)
        self.assertIn("kill -TERM \"$pane_pid\"", text)
        self.assertIn("trap handle_shutdown TERM INT HUP", text)

    def test_docker_stop_timeout_exceeds_entrypoint_grace(self):
        """docker stop must outlast the entrypoint's grace window so bash can flush history."""
        import re
        from skua.commands.stop import DOCKER_STOP_TIMEOUT

        entrypoint = Path(__file__).resolve().parent.parent / "skua" / "container" / "entrypoint.sh"
        text = entrypoint.read_text()
        match = re.search(r'STOP_GRACE="\$\{SKUA_STOP_GRACE:-(\d+)\}"', text)
        self.assertIsNotNone(match, "entrypoint must define STOP_GRACE default")
        grace_default = int(match.group(1))
        self.assertGreater(
            DOCKER_STOP_TIMEOUT, grace_default,
            "docker stop --time must exceed entrypoint STOP_GRACE so history -a can flush",
        )


if __name__ == "__main__":
    unittest.main()
