# SPDX-License-Identifier: BUSL-1.1
"""Tests for `skua restart` and the container-removed wait helper."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestContainerExists(unittest.TestCase):
    @mock.patch("skua.docker.subprocess.run")
    def test_container_exists_true_when_listed(self, mock_run):
        from skua.docker import container_exists

        mock_run.return_value = SimpleNamespace(stdout="abc123\n", returncode=0)
        self.assertTrue(container_exists("skua-demo"))
        cmd = mock_run.call_args.args[0]
        self.assertIn("-aq", cmd)
        self.assertIn("name=^skua-demo$", cmd)

    @mock.patch("skua.docker.subprocess.run")
    def test_container_exists_false_when_empty(self, mock_run):
        from skua.docker import container_exists

        mock_run.return_value = SimpleNamespace(stdout="", returncode=0)
        self.assertFalse(container_exists("skua-demo"))

    @mock.patch("skua.docker.subprocess.run")
    def test_container_exists_uses_ssh_for_remote(self, mock_run):
        from skua.docker import container_exists

        mock_run.return_value = SimpleNamespace(stdout="", returncode=0)
        container_exists("skua-demo", host="builder")
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("builder", cmd)


class TestWaitForContainerRemoved(unittest.TestCase):
    @mock.patch("skua.docker.time.sleep", return_value=None)
    @mock.patch("skua.docker.container_exists")
    def test_returns_true_when_already_gone(self, mock_exists, _sleep):
        from skua.docker import wait_for_container_removed

        mock_exists.return_value = False
        self.assertTrue(wait_for_container_removed("skua-demo", timeout_seconds=1.0))

    @mock.patch("skua.docker.time.sleep", return_value=None)
    @mock.patch("skua.docker.container_exists")
    def test_returns_true_after_container_disappears(self, mock_exists, _sleep):
        from skua.docker import wait_for_container_removed

        mock_exists.side_effect = [True, True, False]
        self.assertTrue(wait_for_container_removed("skua-demo", timeout_seconds=5.0))

    @mock.patch("skua.docker.time.sleep", return_value=None)
    @mock.patch("skua.docker.container_exists", return_value=True)
    def test_returns_false_when_timeout_elapses(self, _mock_exists, _sleep):
        from skua.docker import wait_for_container_removed

        self.assertFalse(wait_for_container_removed("skua-demo", timeout_seconds=0.1))


class TestRestartWaitsForRemoval(unittest.TestCase):
    @mock.patch("skua.commands.restart.cmd_run")
    @mock.patch("skua.commands.restart.wait_for_container_removed")
    @mock.patch("skua.commands.restart.cmd_stop", return_value=True)
    @mock.patch("skua.commands.restart.project_operation_lock")
    @mock.patch("skua.commands.restart.ConfigStore")
    def test_restart_waits_for_container_removal_before_run(
        self,
        mock_store_cls,
        mock_lock,
        mock_stop,
        mock_wait,
        mock_run,
    ):
        from skua.commands.restart import cmd_restart

        store = mock.Mock()
        store.resolve_project.return_value = SimpleNamespace(host="")
        mock_store_cls.return_value = store
        mock_lock.return_value.__enter__ = mock.Mock(return_value=None)
        mock_lock.return_value.__exit__ = mock.Mock(return_value=False)
        mock_wait.return_value = True

        cmd_restart(SimpleNamespace(name="demo", no_attach=True, replace_process=False))

        mock_stop.assert_called_once()
        mock_wait.assert_called_once_with("skua-demo", host="")
        mock_run.assert_called_once()

    @mock.patch("skua.commands.restart.cmd_run")
    @mock.patch("skua.commands.restart.wait_for_container_removed", return_value=False)
    @mock.patch("skua.commands.restart.cmd_stop", return_value=True)
    @mock.patch("skua.commands.restart.project_operation_lock")
    @mock.patch("skua.commands.restart.ConfigStore")
    def test_restart_aborts_run_when_container_lingers(
        self,
        mock_store_cls,
        mock_lock,
        mock_stop,
        _mock_wait,
        mock_run,
    ):
        from skua.commands.restart import cmd_restart

        store = mock.Mock()
        store.resolve_project.return_value = SimpleNamespace(host="")
        mock_store_cls.return_value = store
        mock_lock.return_value.__enter__ = mock.Mock(return_value=None)
        mock_lock.return_value.__exit__ = mock.Mock(return_value=False)

        cmd_restart(SimpleNamespace(name="demo", no_attach=True, replace_process=False))

        mock_stop.assert_called_once()
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
