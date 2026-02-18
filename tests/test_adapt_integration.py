# SPDX-License-Identifier: BUSL-1.1
"""Integration tests for fully automated `skua adapt` workflow."""

import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.commands.adapt import cmd_adapt
from skua.config.loader import ConfigStore
from skua.config.resources import (
    AgentAuthSpec,
    AgentConfig,
    AgentRuntimeSpec,
    Environment,
    Project,
    SecurityProfile,
)
from skua.docker import image_exists, image_name_for_project
from skua.project_adapt import load_image_request


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_docker_available(), "Docker daemon is required for integration tests")
class TestAdaptIntegration(unittest.TestCase):
    def test_cmd_adapt_spins_container_and_applies_agent_suggestion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            project_dir = tmp / "repo"
            project_dir.mkdir()
            (project_dir / "README.md").write_text("integration test\n")

            store = ConfigStore(config_dir=tmp / "cfg")
            store.ensure_dirs()
            store.save_global({"defaults": {"environment": "local-docker", "security": "open", "agent": "mock-agent"}})
            store.save_resource(Environment(name="local-docker"))
            store.save_resource(SecurityProfile(name="open"))
            store.save_resource(
                AgentConfig(
                    name="mock-agent",
                    runtime=AgentRuntimeSpec(
                        command="bash",
                        adapt_command=(
                            "cat > .skua/image-request.yaml <<'EOF'\n"
                            "schemaVersion: 1\n"
                            "status: ready\n"
                            "summary: integration suggestion\n"
                            "baseImage: \"\"\n"
                            "fromImage: \"\"\n"
                            "packages:\n"
                            "  - make\n"
                            "commands:\n"
                            "  - echo integrated\n"
                            "EOF"
                        ),
                    ),
                    auth=AgentAuthSpec(
                        dir=".mock-agent",
                        files=["token"],
                        login_command="mock-agent login",
                    ),
                )
            )
            store.save_resource(
                Project(name="proj", directory=str(project_dir), agent="mock-agent", security="open")
            )

            auth_file = store.project_data_dir("proj", "mock-agent") / "token"
            auth_file.parent.mkdir(parents=True, exist_ok=True)
            auth_file.write_text("ok\n")

            args = argparse.Namespace(
                name="proj",
                base_image="",
                from_image="",
                package=[],
                extra_command=[],
                apply_only=False,
                clear=False,
                write_only=False,
                build=False,
            )

            built_images = []
            try:
                with mock.patch("skua.commands.adapt.ConfigStore", return_value=store):
                    cmd_adapt(args)

                updated = store.load_project("proj")
                self.assertEqual(updated.image.extra_packages, ["make"])
                self.assertEqual(updated.image.extra_commands, ["echo integrated"])
                self.assertEqual(updated.image.version, 1)

                request_path = project_dir / ".skua" / "image-request.yaml"
                applied = load_image_request(request_path)
                self.assertEqual(applied["status"], "applied")

                image_name = image_name_for_project("skua-base", updated)
                built_images.append(image_name)
                self.assertTrue(image_exists(image_name), f"Expected built project image '{image_name}'")
            finally:
                for image in built_images:
                    subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
