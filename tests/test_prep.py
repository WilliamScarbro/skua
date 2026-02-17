# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua prep workflow and image-request helpers."""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.commands.prep import cmd_prep
from skua.config.loader import ConfigStore
from skua.config.resources import AgentConfig, Environment, Project, SecurityProfile
from skua.project_prep import (
    ensure_prep_workspace,
    load_image_request,
    request_has_updates,
    apply_image_request_to_project,
)


class TestProjectPrepHelpers(unittest.TestCase):
    def test_ensure_workspace_creates_guide_and_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "proj"
            project_dir.mkdir()
            guide, request = ensure_prep_workspace(project_dir, "proj", "codex")
            self.assertTrue(guide.is_file())
            self.assertTrue(request.is_file())
            self.assertIn("Skua Image Prep", guide.read_text())
            self.assertIn("schemaVersion: 1", request.read_text())

    def test_request_has_updates_and_apply_to_project(self):
        project = Project(name="p1")
        request = {
            "baseImage": "debian:stable-slim",
            "packages": ["libpq-dev"],
            "commands": ["npm ci"],
        }
        self.assertTrue(request_has_updates(request))
        changed = apply_image_request_to_project(project, request)
        self.assertTrue(changed)
        self.assertEqual(project.image.base_image, "debian:stable-slim")
        self.assertEqual(project.image.extra_packages, ["libpq-dev"])
        self.assertEqual(project.image.extra_commands, ["npm ci"])
        self.assertEqual(project.image.version, 1)


class TestPrepCommand(unittest.TestCase):
    def test_cmd_prep_applies_request_to_project_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = ConfigStore(config_dir=Path(tmpdir) / "cfg")
            store.ensure_dirs()
            store.save_global({"defaults": {"environment": "local-docker", "security": "open", "agent": "codex"}})
            store.save_resource(Environment(name="local-docker"))
            store.save_resource(SecurityProfile(name="open"))
            store.save_resource(AgentConfig(name="codex"))
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            guide, request_path = ensure_prep_workspace(project_dir, "proj", "codex")
            self.assertTrue(guide.exists())
            with open(request_path, "w") as f:
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "fromImage": "ghcr.io/acme/myapp:latest",
                        "packages": ["git", "jq"],
                        "commands": ["echo prepared"],
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

            args = argparse.Namespace(
                name="proj",
                base_image="",
                from_image="",
                package=[],
                extra_command=[],
                clear=False,
                write_only=False,
                build=False,
            )
            with mock.patch("skua.commands.prep.ConfigStore", return_value=store):
                cmd_prep(args)

            updated = store.load_project("proj")
            self.assertEqual(updated.image.from_image, "ghcr.io/acme/myapp:latest")
            self.assertEqual(updated.image.extra_packages, ["git", "jq"])
            self.assertEqual(updated.image.extra_commands, ["echo prepared"])
            self.assertEqual(updated.image.version, 1)

            applied = load_image_request(request_path)
            self.assertEqual(applied["status"], "applied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
