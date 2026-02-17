# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua purge helper selection logic."""

import sys
import unittest
from pathlib import Path
from unittest import mock
import io

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.commands.purge import _repo_from_ref, _repo_from_image_name, _select_images_for_purge
from skua.commands.purge import cmd_purge


class TestPurgeImageSelection(unittest.TestCase):
    def test_repo_from_ref_handles_registry_port(self):
        self.assertEqual(
            _repo_from_ref("localhost:5000/skua-base-codex:latest"),
            "localhost:5000/skua-base-codex",
        )

    def test_repo_from_image_name_handles_tag(self):
        self.assertEqual(
            _repo_from_image_name("myorg/skua-base:dev"),
            "myorg/skua-base",
        )

    def test_select_images_for_purge_matches_base_and_agent_suffixes(self):
        refs = [
            "skua-base:latest",
            "skua-base-codex:latest",
            "myorg/skua-base-claude:latest",
            "ubuntu:latest",
        ]
        selected = _select_images_for_purge(refs, "myorg/skua-base")
        self.assertEqual(
            selected,
            ["skua-base:latest", "skua-base-codex:latest", "myorg/skua-base-claude:latest"],
        )


class TestPurgeNoTargets(unittest.TestCase):
    @mock.patch("skua.commands.purge._docker_lines")
    @mock.patch("skua.commands.purge._run_remove")
    def test_purge_skips_remove_calls_when_no_docker_targets(self, mock_remove, mock_lines):
        # containers, volumes, images, config missing
        mock_lines.side_effect = [[], [], []]
        fake_store = mock.Mock()
        fake_store.global_file.exists.return_value = False
        fake_store.config_dir.exists.return_value = False
        with mock.patch("skua.commands.purge.ConfigStore", return_value=fake_store):
            args = mock.Mock(yes=True)
            cmd_purge(args)
        mock_remove.assert_not_called()

    @mock.patch("skua.commands.purge._docker_lines")
    @mock.patch("skua.commands.purge._run_remove")
    def test_purge_summary_lists_projects(self, mock_remove, mock_lines):
        mock_lines.side_effect = [[], [], []]
        fake_store = mock.Mock()
        fake_store.global_file.exists.return_value = False
        fake_store.config_dir.exists.return_value = True
        fake_store.config_dir.__str__ = mock.Mock(return_value="/tmp/skua")
        fake_store.list_resources.return_value = ["legacy-a", "legacy-b"]
        with mock.patch("skua.commands.purge.ConfigStore", return_value=fake_store):
            args = mock.Mock(yes=True)
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                cmd_purge(args)
        text = out.getvalue()
        self.assertIn("Projects:   2", text)
        self.assertNotIn("Project IDs:", text)
        mock_remove.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
