# SPDX-License-Identifier: BUSL-1.1
"""Tests for project operation locking and persisted project state."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.loader import ConfigStore
from skua.config.resources import Project
from skua.project_lock import (
    ProjectBusyError,
    format_project_busy_error,
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


if __name__ == "__main__":
    unittest.main()
