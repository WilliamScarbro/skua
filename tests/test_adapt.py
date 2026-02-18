# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua adapt workflow and image-request helpers."""

import argparse
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.commands.adapt import cmd_adapt
from skua.config.loader import ConfigStore
from skua.config.resources import AgentConfig, Environment, Project, SecurityProfile
from skua.project_adapt import (
    ensure_adapt_workspace,
    load_image_request,
    request_has_updates,
    apply_image_request_to_project,
)


class TestProjectAdaptHelpers(unittest.TestCase):
    def test_ensure_workspace_creates_guide_and_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "proj"
            project_dir.mkdir()
            guide, request = ensure_adapt_workspace(project_dir, "proj", "codex")
            self.assertTrue(guide.is_file())
            self.assertTrue(request.is_file())
            self.assertIn("Skua Image Adapt", guide.read_text())
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


class TestAdaptCommand(unittest.TestCase):
    def _new_store(self, config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=config_dir)
        store.ensure_dirs()
        store.save_global({"defaults": {"environment": "local-docker", "security": "open", "agent": "codex"}})
        store.save_resource(Environment(name="local-docker"))
        store.save_resource(SecurityProfile(name="open"))
        store.save_resource(AgentConfig(name="codex"))
        return store

    def _write_project_files(self, base_dir: Path, files: dict[str, str]) -> Path:
        base_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in files.items():
            p = base_dir / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return base_dir

    def _adapt_args(self, name: str, apply_only: bool = False, build: bool = False):
        return argparse.Namespace(
            name=name,
            base_image="",
            from_image="",
            package=[],
            extra_command=[],
            apply_only=apply_only,
            clear=False,
            write_only=False,
            build=build,
        )

    def test_cmd_adapt_applies_request_to_project_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            guide, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
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

            args = self._adapt_args("proj", apply_only=True)
            with mock.patch("skua.commands.adapt.ConfigStore", return_value=store):
                cmd_adapt(args)

            updated = store.load_project("proj")
            self.assertEqual(updated.image.from_image, "ghcr.io/acme/myapp:latest")
            self.assertEqual(updated.image.extra_packages, ["git", "jq"])
            self.assertEqual(updated.image.extra_commands, ["echo prepared"])
            self.assertEqual(updated.image.version, 1)

            applied = load_image_request(request_path)
            self.assertEqual(applied["status"], "applied")

    def test_cmd_adapt_applies_expected_changes_for_multiple_fixture_projects(self):
        fixtures = [
            {
                "name": "py-analytics",
                "files": {
                    "requirements.txt": "fastapi==0.116.0\npsycopg[binary]==3.2.9\n",
                    "app/main.py": "print('ready')\n",
                },
                "request": {
                    "schemaVersion": 1,
                    "status": "ready",
                    "summary": "Install libpq for psycopg and bootstrap Python deps.",
                    "baseImage": "python:3.12-slim-bookworm",
                    "packages": ["libpq-dev"],
                    "commands": ["pip install -r requirements.txt"],
                },
                "expected": {
                    "from_image": "",
                    "base_image": "python:3.12-slim-bookworm",
                    "extra_packages": ["libpq-dev"],
                    "extra_commands": ["pip install -r requirements.txt"],
                },
            },
            {
                "name": "node-dashboard",
                "files": {
                    "package.json": (
                        "{\n"
                        '  "name": "node-dashboard",\n'
                        '  "private": true,\n'
                        '  "dependencies": { "esbuild": "^0.25.0", "sqlite3": "^5.1.7" }\n'
                        "}\n"
                    ),
                    "src/index.js": "console.log('ready')\n",
                },
                "request": {
                    "schemaVersion": 1,
                    "status": "ready",
                    "summary": "Use node image and build native npm modules.",
                    "fromImage": "node:22-bookworm-slim",
                    "packages": ["python3", "g++", "make"],
                    "commands": ["npm ci"],
                },
                "expected": {
                    "from_image": "node:22-bookworm-slim",
                    "base_image": "",
                    "extra_packages": ["python3", "g++", "make"],
                    "extra_commands": ["npm ci"],
                },
            },
            {
                "name": "go-worker",
                "files": {
                    "go.mod": (
                        "module example.com/go-worker\n\n"
                        "go 1.23.0\n\n"
                        "require github.com/jackc/pgx/v5 v5.6.0\n"
                    ),
                    "cmd/worker/main.go": (
                        "package main\n\n"
                        "func main() {}\n"
                    ),
                },
                "request": {
                    "schemaVersion": 1,
                    "status": "ready",
                    "summary": "Start from go toolchain image and prefetch modules.",
                    "fromImage": "golang:1.23-bookworm",
                    "packages": ["git", "ca-certificates"],
                    "commands": ["go mod download"],
                },
                "expected": {
                    "from_image": "golang:1.23-bookworm",
                    "base_image": "",
                    "extra_packages": ["git", "ca-certificates"],
                    "extra_commands": ["go mod download"],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._new_store(tmp / "cfg")
            with mock.patch("skua.commands.adapt.ConfigStore", return_value=store):
                for fixture in fixtures:
                    with self.subTest(project=fixture["name"]):
                        project_dir = self._write_project_files(tmp / fixture["name"], fixture["files"])
                        store.save_resource(
                            Project(name=fixture["name"], directory=str(project_dir), agent="codex")
                        )
                        _, request_path = ensure_adapt_workspace(project_dir, fixture["name"], "codex")
                        with open(request_path, "w") as f:
                            yaml.dump(
                                fixture["request"],
                                f,
                                default_flow_style=False,
                                sort_keys=False,
                            )

                        stdout = io.StringIO()
                        with mock.patch("sys.stdout", stdout):
                            cmd_adapt(self._adapt_args(fixture["name"], apply_only=True))
                        output = stdout.getvalue()

                        updated = store.load_project(fixture["name"])
                        self.assertEqual(updated.image.from_image, fixture["expected"]["from_image"])
                        self.assertEqual(updated.image.base_image, fixture["expected"]["base_image"])
                        self.assertEqual(updated.image.extra_packages, fixture["expected"]["extra_packages"])
                        self.assertEqual(updated.image.extra_commands, fixture["expected"]["extra_commands"])
                        self.assertEqual(updated.image.version, 1)

                        applied = load_image_request(request_path)
                        self.assertEqual(applied["status"], "applied")
                        with open(request_path) as f:
                            raw_applied = yaml.safe_load(f) or {}
                        self.assertEqual(raw_applied.get("appliedVersion"), 1)

                        self.assertIn("Applied image request from:", output)
                        self.assertIn("Resolved image config:", output)
                        if fixture["expected"]["from_image"]:
                            self.assertIn(fixture["expected"]["from_image"], output)
                        if fixture["expected"]["base_image"]:
                            self.assertIn(fixture["expected"]["base_image"], output)
                        for pkg in fixture["expected"]["extra_packages"]:
                            self.assertIn(pkg, output)

    def test_cmd_adapt_runs_agent_session_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "packages": ["git"],
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session") as mock_session,
                mock.patch("skua.commands.adapt._build_project_image") as mock_build,
            ):
                cmd_adapt(self._adapt_args("proj"))

            mock_session.assert_called_once()
            mock_build.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
