#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
"""Tests for `skua remove` local and remote cleanup behavior."""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.loader import ConfigStore
from skua.config.resources import Environment, Project


class TestRemoveCommand(unittest.TestCase):
    def _args(self, name: str):
        return argparse.Namespace(name=name)

    def test_remove_remote_project_cleans_remote_resources(self):
        from skua.commands.remove import cmd_remove

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_global({"imageName": "skua-base"})
            store.save_resource(Environment(name="local-docker"))
            store.save_resource(
                Project(
                    name="qar",
                    environment="local-docker",
                    agent="claude",
                    repo="git@github.com:org/repo.git",
                    host="docker.example.com",
                )
            )

            with mock.patch("skua.commands.remove.ConfigStore", return_value=store):
                with mock.patch("skua.commands.run._ensure_local_ssh_client_for_remote_docker"):
                    with mock.patch("skua.commands.run._configure_remote_docker_transport"):
                        with mock.patch("skua.commands.remove.is_container_running", return_value=False):
                            with mock.patch("skua.commands.remove.confirm", return_value=True):
                                with mock.patch("skua.commands.remove.image_name_for_project", return_value="skua-base-claude"):
                                    with mock.patch("skua.commands.remove._run_docker_remove") as mock_remove:
                                        cmd_remove(self._args("qar"))
                                        calls = [c.args[0] for c in mock_remove.call_args_list]
                                        self.assertIn(["docker", "rm", "-f", "skua-qar"], calls)
                                        self.assertIn(["docker", "volume", "rm", "skua-qar-claude"], calls)
                                        self.assertIn(["docker", "volume", "rm", "skua-qar-repo"], calls)
                                        self.assertIn(["docker", "image", "rm", "-f", "skua-base-claude"], calls)
                                        self.assertIsNone(store.load_project("qar"))

    def test_remove_remote_running_container_cancelled_by_user(self):
        from skua.commands.remove import cmd_remove

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_global({"imageName": "skua-base"})
            store.save_resource(Environment(name="local-docker"))
            store.save_resource(
                Project(
                    name="qar",
                    environment="local-docker",
                    agent="claude",
                    host="docker.example.com",
                )
            )

            with mock.patch("skua.commands.remove.ConfigStore", return_value=store):
                with mock.patch("skua.commands.run._ensure_local_ssh_client_for_remote_docker"):
                    with mock.patch("skua.commands.run._configure_remote_docker_transport"):
                        with mock.patch("skua.commands.remove.is_container_running", return_value=True):
                            with mock.patch("skua.commands.remove.confirm", return_value=False):
                                with mock.patch("skua.commands.remove._run_docker_remove") as mock_remove:
                                    cmd_remove(self._args("qar"))
                                    mock_remove.assert_not_called()
                                    self.assertIsNotNone(store.load_project("qar"))

    def test_remove_local_bind_project_deletes_data_dir_when_confirmed(self):
        from skua.commands.remove import cmd_remove

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_resource(Environment(name="local-docker"))
            store.save_resource(
                Project(
                    name="localproj",
                    environment="local-docker",
                    agent="claude",
                    host="",
                )
            )
            data_dir = store.project_data_dir("localproj", "claude")
            data_dir.mkdir(parents=True)
            (data_dir / "auth.json").write_text("{}")

            with mock.patch("skua.commands.remove.ConfigStore", return_value=store):
                with mock.patch("skua.commands.remove.is_container_running", return_value=False):
                    with mock.patch("skua.commands.remove.confirm", return_value=True):
                        cmd_remove(self._args("localproj"))
                        self.assertFalse(data_dir.exists())
                        self.assertIsNone(store.load_project("localproj"))


if __name__ == "__main__":
    unittest.main()
