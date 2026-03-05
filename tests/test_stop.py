#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
"""Tests for `skua stop` git safety checks."""

import sys
import tempfile
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
