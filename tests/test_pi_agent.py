# SPDX-License-Identifier: BUSL-1.1
"""Tests for the bundled pi agent preset."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.commands.adapt import _agent_adapt_command
from skua.commands.credential import agent_default_source_dir, resolve_credential_sources
from skua.config import ConfigStore
from skua.config.resources import (
    AgentAuthSpec,
    AgentConfig,
    AgentRuntimeSpec,
    resource_from_dict,
)


PRESET_DIR = Path(__file__).resolve().parent.parent / "skua" / "presets"
PI_PRESET = PRESET_DIR / "agents" / "pi.yaml"


def _pi_agent() -> AgentConfig:
    return AgentConfig(
        name="pi",
        runtime=AgentRuntimeSpec(
            command="pi",
            adapt_command="pi -p --no-session {prompt_shell}",
        ),
        auth=AgentAuthSpec(
            dir=".pi/agent",
            files=["auth.json"],
            login_command="pi --login",
        ),
    )


class TestPiPresetDiscovery(unittest.TestCase):
    def test_pi_preset_file_ships_with_package(self):
        self.assertTrue(PI_PRESET.is_file(), f"Missing preset file: {PI_PRESET}")

    def test_pi_preset_parses_as_agent_config(self):
        with open(PI_PRESET) as f:
            data = yaml.safe_load(f)

        agent = resource_from_dict(data)

        self.assertIsInstance(agent, AgentConfig)
        self.assertEqual(agent.name, "pi")
        self.assertEqual(agent.runtime.command, "pi")
        self.assertEqual(agent.auth.dir, ".pi/agent")
        self.assertEqual(agent.auth.files, ["auth.json"])
        self.assertEqual(agent.auth.login_command, "pi --login")
        self.assertIn("pi -p --no-session", agent.runtime.adapt_command)

    def test_install_presets_copies_pi_preset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir) / "cfg")
            store.install_presets(PRESET_DIR)

            loaded = store.load_agent("pi")
            self.assertIsNotNone(loaded, "pi preset was not installed")
            self.assertEqual(loaded.auth.dir, ".pi/agent")


class TestPiCredentialResolution(unittest.TestCase):
    @mock.patch("skua.commands.credential.Path.home")
    def test_default_source_dir_is_nested_pi_agent(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mock_home.return_value = home

            src_dir = agent_default_source_dir(_pi_agent())

            self.assertEqual(src_dir, home / ".pi" / "agent")

    @mock.patch("skua.commands.credential.Path.home")
    def test_resolve_sources_uses_pi_auth_json(self, mock_home):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            auth_dir = home / ".pi" / "agent"
            auth_dir.mkdir(parents=True)
            (auth_dir / "auth.json").write_text('{"token":"pi"}')
            mock_home.return_value = home

            sources = resolve_credential_sources(None, _pi_agent())

            self.assertEqual(len(sources), 1)
            src, dest = sources[0]
            self.assertEqual(src, auth_dir / "auth.json")
            self.assertEqual(dest, "auth.json")


class TestPiAdaptCommand(unittest.TestCase):
    def test_adapt_command_renders_non_interactive_argv(self):
        argv = _agent_adapt_command(_pi_agent(), project_name="demo", prompt_override="hello")

        self.assertEqual(argv[0], "pi")
        self.assertIn("-p", argv)
        self.assertIn("--no-session", argv)
        self.assertIn("hello", argv)


if __name__ == "__main__":
    unittest.main()
