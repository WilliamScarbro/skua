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
                                with mock.patch("skua.commands.remove._run_docker_remove") as mock_remove:
                                    cmd_remove(self._args("qar"))
                                    calls = [c.args[0] for c in mock_remove.call_args_list]
                                    self.assertIn(["docker", "rm", "-f", "skua-qar"], calls)
                                    self.assertIn(["docker", "volume", "rm", "skua-qar-claude"], calls)
                                    self.assertIn(["docker", "volume", "rm", "skua-qar-repo"], calls)
                                    self.assertIsNone(store.load_project("qar"))

    def test_remove_skips_base_agent_image(self):
        """Base images (skua-base-claude, skua-base-codex) are always protected, even when only one project exists."""
        from skua.commands.remove import cmd_remove
        from skua.config.resources import ProjectResourcesSpec

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_global({"imageName": "skua-base"})
            store.save_resource(Environment(name="local-docker"))
            project = Project(
                name="baseproj",
                environment="local-docker",
                agent="claude",
            )
            project.resources = ProjectResourcesSpec(images=["skua-base-claude"])
            store.save_resource(project)

            with mock.patch("skua.commands.remove.ConfigStore", return_value=store):
                with mock.patch("skua.commands.remove.is_container_running", return_value=False):
                    with mock.patch("skua.commands.remove.confirm", return_value=True):
                        with mock.patch("skua.commands.remove._run_docker_remove") as mock_remove:
                            cmd_remove(self._args("baseproj"))
                            image_calls = [c.args[0] for c in mock_remove.call_args_list
                                           if "rmi" in c.args[0] or "image" in c.args[0]]
                            self.assertEqual([], image_calls, "Base image must not be deleted on project remove")

    def test_remove_skips_image_shared_by_another_project(self):
        """Any image still referenced by another project must not be deleted."""
        from skua.commands.remove import cmd_remove
        from skua.config.resources import ProjectResourcesSpec

        shared_image = "skua-base-claude-shared-v1"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_global({"imageName": "skua-base"})
            store.save_resource(Environment(name="local-docker"))

            proj_a = Project(name="proj-a", environment="local-docker", agent="claude")
            proj_a.resources = ProjectResourcesSpec(images=[shared_image])
            store.save_resource(proj_a)

            proj_b = Project(name="proj-b", environment="local-docker", agent="claude")
            proj_b.resources = ProjectResourcesSpec(images=[shared_image])
            store.save_resource(proj_b)

            with mock.patch("skua.commands.remove.ConfigStore", return_value=store):
                with mock.patch("skua.commands.remove.is_container_running", return_value=False):
                    with mock.patch("skua.commands.remove.confirm", return_value=True):
                        with mock.patch("skua.commands.remove._run_docker_remove") as mock_remove:
                            cmd_remove(self._args("proj-a"))
                            image_calls = [c.args[0] for c in mock_remove.call_args_list
                                           if "rmi" in c.args[0]]
                            self.assertEqual([], image_calls, "Shared image must not be deleted while proj-b still uses it")
                            self.assertIsNone(store.load_project("proj-a"))
                            self.assertIsNotNone(store.load_project("proj-b"))

    def test_remove_deletes_project_specific_image(self):
        """Project-specific images with no other referencing project should be deleted."""
        from skua.commands.remove import cmd_remove
        from skua.config.resources import ProjectResourcesSpec

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_global({"imageName": "skua-base"})
            store.save_resource(Environment(name="local-docker"))
            project = Project(
                name="customproj",
                environment="local-docker",
                agent="claude",
            )
            project.resources = ProjectResourcesSpec(images=["skua-base-claude-customproj-v1"])
            store.save_resource(project)

            with mock.patch("skua.commands.remove.ConfigStore", return_value=store):
                with mock.patch("skua.commands.remove.is_container_running", return_value=False):
                    with mock.patch("skua.commands.remove.confirm", return_value=True):
                        with mock.patch("skua.commands.remove._run_docker_remove") as mock_remove:
                            cmd_remove(self._args("customproj"))
                            calls = [c.args[0] for c in mock_remove.call_args_list]
                            self.assertIn(["docker", "rmi", "skua-base-claude-customproj-v1"], calls)

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
