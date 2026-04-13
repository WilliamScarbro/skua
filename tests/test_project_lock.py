# SPDX-License-Identifier: BUSL-1.1
"""Tests for project operation locking and persisted project state."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.loader import ConfigStore
from skua.config.resources import Project
from skua.project_lock import (
    ProjectBusyError,
    effective_project_operation_state,
    format_project_busy_error,
    project_busy_error_if_locked,
    project_operation_lock,
)


class TestProjectOperationLock(unittest.TestCase):
    def test_lock_sets_and_clears_project_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_resource(Project(name="demo", directory="/tmp/demo"))

            with project_operation_lock(store, "demo", "building"):
                locked = store.load_project("demo")
                self.assertEqual("building", locked.state.status)
                self.assertTrue(locked.state.lock_owner)
                self.assertTrue(locked.state.lock_acquired_at)

            unlocked = store.load_project("demo")
            self.assertEqual("", unlocked.state.status)
            self.assertEqual("", unlocked.state.lock_owner)
            self.assertEqual("", unlocked.state.lock_acquired_at)

    def test_lock_contention_raises_project_busy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            other = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_resource(Project(name="demo", directory="/tmp/demo"))

            with project_operation_lock(store, "demo", "adapting"):
                with self.assertRaises(ProjectBusyError) as ctx:
                    with project_operation_lock(other, "demo", "building"):
                        pass

            self.assertEqual("demo", ctx.exception.project_name)

    def test_effective_state_clears_stale_persisted_operation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            project = Project(name="demo", directory="/tmp/demo")
            project.state.status = "building"
            project.state.lock_owner = "host:1234"
            project.state.lock_acquired_at = "2026-03-06T00:00:00+00:00"
            store.save_resource(project)

            loaded = store.load_project("demo")
            self.assertEqual("", effective_project_operation_state(store, loaded))

            refreshed = store.load_project("demo")
            self.assertEqual("", refreshed.state.status)
            self.assertEqual("", refreshed.state.lock_owner)
            self.assertEqual("", refreshed.state.lock_acquired_at)

    def test_busy_error_format_includes_context(self):
        err = ProjectBusyError(
            project_name="demo",
            operation="stopping",
            owner="host:999",
            acquired_at="2026-03-06T00:00:00+00:00",
        )
        msg = format_project_busy_error(err, "stop this project")
        self.assertIn("Project 'demo' is busy", msg)
        self.assertIn("stopping", msg)
        self.assertIn("host:999", msg)
        self.assertIn("cannot stop this project", msg)

    def test_run_clears_starting_lock_before_attach(self):
        from skua.commands.run import cmd_run

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_resource(Project(name="demo", directory="/tmp/demo", agent="claude"))

            environment = mock.Mock(persistence=mock.Mock(mode="bind"), network=mock.Mock(mode="open"))
            security = mock.Mock()
            agent = mock.Mock(name="claude", auth=mock.Mock(dir=".claude"))
            agent.name = "claude"
            agent.auth.dir = ".claude"
            agent.auth.files = []
            store.load_environment = mock.Mock(return_value=environment)
            store.load_security = mock.Mock(return_value=security)
            store.load_agent = mock.Mock(return_value=agent)
            store.load_global = mock.Mock(return_value={
                "imageName": "skua-base",
                "baseImage": "debian:bookworm-slim",
                "defaults": {"security": "open"},
                "image": {},
            })
            store.project_data_dir = mock.Mock(return_value=Path(tmpdir) / "data")
            store.get_container_dir = mock.Mock(return_value=Path(tmpdir) / "container")
            store.load_credential = mock.Mock(return_value=None)
            store.refresh_agent_preset = mock.Mock()

            with mock.patch("skua.commands.run.ConfigStore", return_value=store):
                with mock.patch("skua.commands.run.validate_project", return_value=SimpleNamespace(valid=True, warnings=[], errors=[])):
                    with mock.patch("skua.commands.run.resolve_project_image_inputs", return_value=("debian:bookworm-slim", [], [])):
                        with mock.patch("skua.commands.run.image_name_for_project", return_value="skua-base-claude"):
                            with mock.patch("skua.commands.run.image_rebuild_needed", return_value=(False, False, "")):
                                with mock.patch("skua.commands.run.image_exists", return_value=True):
                                    with mock.patch("skua.commands.run.ensure_adapt_workspace"):
                                        with mock.patch("skua.commands.run._maybe_refresh_local_credentials", return_value=False):
                                            with mock.patch("skua.commands.run._seed_auth_from_host", return_value=0):
                                                with mock.patch("skua.commands.run.build_run_command", return_value=["docker", "run"]):
                                                    with mock.patch("skua.commands.run.start_container", return_value=True):
                                                        with mock.patch("skua.commands.run.wait_for_running_container", return_value=True):
                                                            with mock.patch("skua.commands.run.exec_into_container", return_value=True) as mock_attach:
                                                                cmd_run(SimpleNamespace(name="demo", replace_process=False))

            self.assertEqual(1, mock_attach.call_count)
            self.assertIsNone(project_busy_error_if_locked(store, "demo"))
            project = store.load_project("demo")
            self.assertEqual("", project.state.status)


if __name__ == "__main__":
    unittest.main()
