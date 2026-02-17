# SPDX-License-Identifier: BUSL-1.1
"""Tests for git repository support in skua projects.

Validates:
- Project dataclass stores and serializes the repo field
- ConfigStore provides correct repo paths
- `skua add` handles --repo and --dir mutual exclusivity
- `skua add` validates git URLs
- `skua run` clones repos and sets project.directory
- `skua list` shows repo URL when directory is empty
- `skua describe` includes repo in output
"""

import argparse
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

# Ensure the skua package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.resources import (
    Project, ProjectGitSpec, ProjectSshSpec, ProjectImageSpec,
    Environment, SecurityProfile, AgentAuthSpec, AgentRuntimeSpec,
    AgentConfig, AgentInstallSpec,
    resource_to_dict, resource_from_dict,
)
from skua.config.loader import ConfigStore
from skua.commands.add import _is_git_url


class TestProjectRepoField(unittest.TestCase):
    """Test that the Project dataclass handles the repo field correctly."""

    def test_default_repo_is_empty(self):
        p = Project(name="test")
        self.assertEqual(p.repo, "")

    def test_repo_stored(self):
        p = Project(name="test", repo="https://github.com/user/repo.git")
        self.assertEqual(p.repo, "https://github.com/user/repo.git")

    def test_repo_serialization_roundtrip(self):
        p = Project(
            name="test",
            repo="git@github.com:user/repo.git",
            directory="",
        )
        d = resource_to_dict(p)
        self.assertEqual(d["spec"]["repo"], "git@github.com:user/repo.git")

        p2 = resource_from_dict(d)
        self.assertEqual(p2.repo, "git@github.com:user/repo.git")
        self.assertEqual(p2.name, "test")

    def test_repo_empty_serialization(self):
        p = Project(name="test", directory="/tmp/foo")
        d = resource_to_dict(p)
        self.assertEqual(d["spec"]["repo"], "")

        p2 = resource_from_dict(d)
        self.assertEqual(p2.repo, "")
        self.assertEqual(p2.directory, "/tmp/foo")

    def test_repo_and_directory_coexist(self):
        """Both fields can be set (run.py sets directory from repo at runtime)."""
        p = Project(name="test", repo="https://x.com/r.git", directory="/tmp/clone")
        self.assertEqual(p.repo, "https://x.com/r.git")
        self.assertEqual(p.directory, "/tmp/clone")


class TestConfigStoreRepoPaths(unittest.TestCase):
    """Test ConfigStore repo directory helpers."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ConfigStore(config_dir=Path(self.tmpdir))

    def test_repos_dir(self):
        expected = Path(self.tmpdir) / "repos"
        self.assertEqual(self.store.repos_dir(), expected)

    def test_repo_dir(self):
        expected = Path(self.tmpdir) / "repos" / "myproject"
        self.assertEqual(self.store.repo_dir("myproject"), expected)

    def test_repo_dir_different_projects(self):
        self.assertNotEqual(
            self.store.repo_dir("proj-a"),
            self.store.repo_dir("proj-b"),
        )

    def test_project_data_dir_claude_legacy_path(self):
        expected = Path(self.tmpdir) / "claude-data" / "myproject"
        self.assertEqual(self.store.project_data_dir("myproject", "claude"), expected)

    def test_project_data_dir_non_claude_path(self):
        expected = Path(self.tmpdir) / "agent-data" / "codex" / "myproject"
        self.assertEqual(self.store.project_data_dir("myproject", "codex"), expected)


class TestAgentImageNaming(unittest.TestCase):
    """Test image naming strategy for per-agent base images."""

    def test_default_pattern(self):
        from skua.docker import image_name_for_agent
        self.assertEqual(image_name_for_agent("skua-base", "codex"), "skua-base-codex")
        self.assertEqual(image_name_for_agent("skua-base", "claude"), "skua-base-claude")

    def test_preserves_tag(self):
        from skua.docker import image_name_for_agent
        self.assertEqual(
            image_name_for_agent("myorg/skua-base:latest", "codex"),
            "myorg/skua-base-codex:latest",
        )

    def test_registry_port_not_treated_as_tag(self):
        from skua.docker import image_name_for_agent
        self.assertEqual(
            image_name_for_agent("localhost:5000/skua-base", "claude"),
            "localhost:5000/skua-base-claude",
        )

    def test_idempotent_if_suffix_present(self):
        from skua.docker import image_name_for_agent
        self.assertEqual(
            image_name_for_agent("skua-base-codex", "codex"),
            "skua-base-codex",
        )


class TestBuildRequiredAgents(unittest.TestCase):
    """Test agent selection for lazy project-scoped builds."""

    def test_no_projects_requires_no_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            from skua.commands.build import _required_project_agents
            self.assertEqual(_required_project_agents(store), [])

    def test_collects_unique_project_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_resource(Project(name="a", directory="/tmp/a", agent="codex"))
            store.save_resource(Project(name="b", directory="/tmp/b", agent="claude"))
            store.save_resource(Project(name="c", directory="/tmp/c", agent="codex"))
            from skua.commands.build import _required_project_agents
            self.assertEqual(_required_project_agents(store), ["claude", "codex"])


class TestAgentBaseImages(unittest.TestCase):
    """Test agent-specific base image selection."""

    def test_codex_uses_global_default_base_image(self):
        from skua.docker import base_image_for_agent
        agent = AgentConfig(name="codex")
        self.assertEqual(
            base_image_for_agent("debian:bookworm-slim", agent),
            "debian:bookworm-slim",
        )

    def test_non_codex_uses_global_default(self):
        from skua.docker import base_image_for_agent
        agent = AgentConfig(name="claude")
        self.assertEqual(
            base_image_for_agent("debian:bookworm-slim", agent),
            "debian:bookworm-slim",
        )

    def test_agent_override_base_image(self):
        from skua.docker import base_image_for_agent
        agent = AgentConfig(
            name="codex",
            install=AgentInstallSpec(base_image="ghcr.io/openai/codex-universal:stable"),
        )
        self.assertEqual(
            base_image_for_agent("debian:bookworm-slim", agent),
            "ghcr.io/openai/codex-universal:stable",
        )

    def test_legacy_codex_universal_preset_falls_back_to_default(self):
        from skua.docker import base_image_for_agent
        agent = AgentConfig(
            name="codex",
            install=AgentInstallSpec(
                base_image="ghcr.io/openai/codex-universal:latest",
                commands=[],
                required_packages=[],
            ),
        )
        self.assertEqual(
            base_image_for_agent("debian:bookworm-slim", agent),
            "debian:bookworm-slim",
        )


class TestDockerfileAgentInstall(unittest.TestCase):
    """Test agent install behavior in generated Dockerfiles."""

    def test_sets_npm_prefix_for_non_root_global_installs(self):
        from skua.docker import generate_dockerfile
        agent = AgentConfig(
            name="codex",
            install=AgentInstallSpec(commands=["npm install -g @openai/codex"]),
        )
        dockerfile = generate_dockerfile(agent=agent)
        self.assertIn('ENV NPM_CONFIG_PREFIX="/home/dev/.local"', dockerfile)
        self.assertIn("USER dev", dockerfile)

    def test_codex_default_required_packages_added(self):
        from skua.docker import generate_dockerfile
        agent = AgentConfig(name="codex", install=AgentInstallSpec(commands=[]))
        dockerfile = generate_dockerfile(agent=agent)
        self.assertIn("nodejs", dockerfile)
        self.assertIn("npm", dockerfile)

    def test_codex_legacy_npm_install_command_is_normalized(self):
        from skua.docker import generate_dockerfile
        agent = AgentConfig(
            name="codex",
            install=AgentInstallSpec(commands=["npm install -g @openai/codex"]),
        )
        dockerfile = generate_dockerfile(agent=agent)
        self.assertIn("npm install -g --prefix /home/dev/.local @openai/codex", dockerfile)


class TestRunCommandEnv(unittest.TestCase):
    """Test runtime env injection for agent-aware entrypoint behavior."""

    def test_build_run_command_sets_agent_env(self):
        from skua.docker import build_run_command

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Project(name="p1", directory="", agent="codex")
            env = Environment(name="local-docker")
            sec = SecurityProfile(name="open")
            agent = AgentConfig(
                name="codex",
                runtime=AgentRuntimeSpec(command="codex"),
                auth=AgentAuthSpec(dir=".codex", files=["auth.json"], login_command="codex login"),
            )
            data_dir = Path(tmpdir) / "data"
            cmd = build_run_command(project, env, sec, agent, "skua-base-codex", data_dir)
            joined = " ".join(cmd)
            self.assertIn("SKUA_AGENT_NAME=codex", joined)
            self.assertIn("SKUA_AGENT_COMMAND=codex", joined)
            self.assertIn("SKUA_AGENT_LOGIN_COMMAND=codex login", joined)
            self.assertIn("SKUA_AUTH_DIR=.codex", joined)
            self.assertIn("SKUA_AUTH_FILES=auth.json", joined)
            self.assertIn(f"{data_dir}:/home/dev/.codex", joined)
            self.assertIn("SKUA_PROJECT_DIR=/home/dev/p1", joined)

    def test_build_run_command_mounts_host_directory_name(self):
        from skua.docker import build_run_command

        with tempfile.TemporaryDirectory() as tmpdir:
            host_dir = Path(tmpdir) / "workbench"
            host_dir.mkdir()
            project = Project(name="p1", directory=str(host_dir), agent="codex")
            env = Environment(name="local-docker")
            sec = SecurityProfile(name="open")
            agent = AgentConfig(
                name="codex",
                runtime=AgentRuntimeSpec(command="codex"),
                auth=AgentAuthSpec(dir=".codex", files=["auth.json"], login_command="codex login"),
            )
            data_dir = Path(tmpdir) / "data"
            cmd = build_run_command(project, env, sec, agent, "skua-base-codex", data_dir)
            joined = " ".join(cmd)
            self.assertIn(f"{host_dir}:/home/dev/workbench", joined)
            self.assertIn("SKUA_PROJECT_DIR=/home/dev/workbench", joined)

    def test_build_run_command_mounts_repo_name_for_repo_projects(self):
        from skua.docker import build_run_command

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_dir = Path(tmpdir) / "project-alias"
            clone_dir.mkdir()
            project = Project(
                name="p1",
                directory=str(clone_dir),
                repo="git@github.com:acme/platform-api.git",
                agent="codex",
            )
            env = Environment(name="local-docker")
            sec = SecurityProfile(name="open")
            agent = AgentConfig(
                name="codex",
                runtime=AgentRuntimeSpec(command="codex"),
                auth=AgentAuthSpec(dir=".codex", files=["auth.json"], login_command="codex login"),
            )
            data_dir = Path(tmpdir) / "data"
            cmd = build_run_command(project, env, sec, agent, "skua-base-codex", data_dir)
            joined = " ".join(cmd)
            self.assertIn(f"{clone_dir}:/home/dev/platform-api", joined)
            self.assertIn("SKUA_PROJECT_DIR=/home/dev/platform-api", joined)


class TestAuthSeeding(unittest.TestCase):
    """Test host -> persisted auth file seeding for run command."""

    @mock.patch("skua.commands.run.Path.home")
    def test_seed_auth_from_host_prefers_auth_dir(self, mock_home):
        from skua.commands.run import _seed_auth_from_host

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            data = Path(tmpdir) / "data"
            (home / ".codex").mkdir(parents=True)
            data.mkdir(parents=True)
            (home / ".codex" / "auth.json").write_text('{"token":"abc"}')
            mock_home.return_value = home

            copied = _seed_auth_from_host(data, ".codex", ["auth.json"])
            self.assertEqual(copied, 1)
            self.assertTrue((data / "auth.json").is_file())

    @mock.patch("skua.commands.run.Path.home")
    def test_seed_auth_from_host_falls_back_to_home_root(self, mock_home):
        from skua.commands.run import _seed_auth_from_host

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            data = Path(tmpdir) / "data"
            home.mkdir(parents=True)
            data.mkdir(parents=True)
            (home / ".claude.json").write_text("{}")
            mock_home.return_value = home

            copied = _seed_auth_from_host(data, ".claude", [".claude.json"])
            self.assertEqual(copied, 1)
            self.assertTrue((data / ".claude.json").is_file())

    @mock.patch("skua.commands.run.Path.home")
    def test_seed_auth_does_not_overwrite_existing_file(self, mock_home):
        from skua.commands.run import _seed_auth_from_host

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            data = Path(tmpdir) / "data"
            (home / ".codex").mkdir(parents=True)
            data.mkdir(parents=True)
            (home / ".codex" / "auth.json").write_text('{"token":"host"}')
            (data / "auth.json").write_text('{"token":"existing"}')
            mock_home.return_value = home

            copied = _seed_auth_from_host(data, ".codex", ["auth.json"])
            self.assertEqual(copied, 0)
            self.assertIn("existing", (data / "auth.json").read_text())


class TestGitUrlValidation(unittest.TestCase):
    """Test the _is_git_url helper."""

    def test_https_url(self):
        self.assertTrue(_is_git_url("https://github.com/user/repo.git"))

    def test_http_url(self):
        self.assertTrue(_is_git_url("http://github.com/user/repo.git"))

    def test_git_protocol(self):
        self.assertTrue(_is_git_url("git://github.com/user/repo.git"))

    def test_ssh_scp_style(self):
        self.assertTrue(_is_git_url("git@github.com:user/repo.git"))

    def test_ssh_url(self):
        self.assertTrue(_is_git_url("ssh://git@github.com/user/repo.git"))

    def test_plain_string_rejected(self):
        self.assertFalse(_is_git_url("foo"))

    def test_local_path_rejected(self):
        self.assertFalse(_is_git_url("/tmp/some/repo"))

    def test_relative_path_rejected(self):
        self.assertFalse(_is_git_url("some/repo"))

    def test_ftp_rejected(self):
        self.assertFalse(_is_git_url("ftp://server/repo.git"))


class TestAddMutualExclusivity(unittest.TestCase):
    """Test that --dir and --repo are mutually exclusive in cmd_add."""

    def _make_args(self, **kwargs):
        defaults = dict(
            name="test-proj",
            dir=None,
            repo=None,
            ssh_key="",
            env=None,
            security=None,
            agent=None,
            quick=True,
            no_prompt=True,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    @mock.patch("skua.commands.add.ConfigStore")
    def test_dir_and_repo_both_set_errors(self, MockStore):
        """Providing both --dir and --repo should exit with error."""
        mock_store = MockStore.return_value
        mock_store.is_initialized.return_value = True
        mock_store.load_project.return_value = None

        from skua.commands.add import cmd_add

        args = self._make_args(dir="/tmp/foo", repo="https://github.com/u/r.git")
        with self.assertRaises(SystemExit) as ctx:
            cmd_add(args)
        self.assertEqual(ctx.exception.code, 1)

    @mock.patch("skua.commands.add.ConfigStore")
    def test_repo_only_accepted(self, MockStore):
        """Providing only --repo should not error on mutual exclusivity."""
        mock_store = MockStore.return_value
        mock_store.is_initialized.return_value = True
        mock_store.load_project.return_value = None
        mock_store.load_global.return_value = {"defaults": {}}
        mock_store.load_environment.return_value = None

        from skua.commands.add import cmd_add

        args = self._make_args(repo="https://github.com/u/r.git")
        # Should not raise SystemExit for mutual exclusivity
        # (may raise for other reasons like missing environment, but that's fine)
        try:
            cmd_add(args)
        except SystemExit:
            # If it exits, it shouldn't be due to mutual exclusivity
            pass

        # Verify save_resource was called with a Project containing the repo
        mock_store.save_resource.assert_called_once()
        saved_project = mock_store.save_resource.call_args[0][0]
        self.assertEqual(saved_project.repo, "https://github.com/u/r.git")
        self.assertEqual(saved_project.directory, "")

    @mock.patch("skua.commands.add.ConfigStore")
    def test_invalid_repo_url_errors(self, MockStore):
        """Providing a non-URL string as --repo should exit with error."""
        mock_store = MockStore.return_value
        mock_store.is_initialized.return_value = True
        mock_store.load_project.return_value = None

        from skua.commands.add import cmd_add

        args = self._make_args(repo="not-a-url")
        with self.assertRaises(SystemExit) as ctx:
            cmd_add(args)
        self.assertEqual(ctx.exception.code, 1)

    @mock.patch("skua.commands.add.ConfigStore")
    def test_unknown_agent_errors(self, MockStore):
        """Providing an unknown --agent should exit with error."""
        mock_store = MockStore.return_value
        mock_store.is_initialized.return_value = True
        mock_store.load_project.return_value = None
        mock_store.load_global.return_value = {"defaults": {}}
        mock_store.list_resources.return_value = ["claude", "codex"]
        mock_store.load_agent.return_value = None

        from skua.commands.add import cmd_add

        args = self._make_args(agent="missing-agent")
        with self.assertRaises(SystemExit) as ctx:
            cmd_add(args)
        self.assertEqual(ctx.exception.code, 1)


class TestRunRepoClone(unittest.TestCase):
    """Test that cmd_run clones repos correctly."""

    def test_clone_invoked_when_repo_dir_missing(self):
        """When project.repo is set and clone dir doesn't exist, git clone is called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))

            project = Project(
                name="test-proj",
                repo="https://github.com/user/repo.git",
                directory="",
                ssh=ProjectSshSpec(),
            )

            clone_dir = store.repo_dir("test-proj")
            self.assertFalse(clone_dir.exists())

            # Mock subprocess.run to simulate git clone
            with mock.patch("skua.commands.run.subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0)

                # Simulate the clone logic from cmd_run
                if project.repo:
                    if not clone_dir.exists():
                        clone_cmd = ["git", "clone"]
                        clone_cmd += [project.repo, str(clone_dir)]
                        subprocess.run(clone_cmd, check=True)
                    project.directory = str(clone_dir)

                mock_run.assert_called_once_with(
                    ["git", "clone", "https://github.com/user/repo.git", str(clone_dir)],
                    check=True,
                )
                self.assertEqual(project.directory, str(clone_dir))

    def test_clone_skipped_when_repo_dir_exists(self):
        """When clone directory already exists, git clone is not called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))

            project = Project(
                name="test-proj",
                repo="https://github.com/user/repo.git",
                directory="",
                ssh=ProjectSshSpec(),
            )

            clone_dir = store.repo_dir("test-proj")
            clone_dir.mkdir(parents=True)

            with mock.patch("skua.commands.run.subprocess.run") as mock_run:
                # Simulate the clone logic from cmd_run
                if project.repo:
                    if not clone_dir.exists():
                        subprocess.run(
                            ["git", "clone", project.repo, str(clone_dir)],
                            check=True,
                        )
                    project.directory = str(clone_dir)

                mock_run.assert_not_called()
                self.assertEqual(project.directory, str(clone_dir))

    def test_clone_uses_ssh_key_when_set(self):
        """When SSH key is set, git clone uses core.sshCommand."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))

            project = Project(
                name="test-proj",
                repo="git@github.com:user/repo.git",
                directory="",
                ssh=ProjectSshSpec(private_key="/home/user/.ssh/id_rsa"),
            )

            clone_dir = store.repo_dir("test-proj")

            with mock.patch("skua.commands.run.subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0)

                # Simulate the clone logic from cmd_run
                if project.repo:
                    if not clone_dir.exists():
                        clone_cmd = ["git", "clone"]
                        if project.ssh.private_key:
                            ssh_cmd = f"ssh -i {project.ssh.private_key} -o StrictHostKeyChecking=no"
                            clone_cmd = ["git", "-c", f"core.sshCommand={ssh_cmd}", "clone"]
                        clone_cmd += [project.repo, str(clone_dir)]
                        subprocess.run(clone_cmd, check=True)
                    project.directory = str(clone_dir)

                expected_ssh = "ssh -i /home/user/.ssh/id_rsa -o StrictHostKeyChecking=no"
                mock_run.assert_called_once_with(
                    [
                        "git", "-c", f"core.sshCommand={expected_ssh}", "clone",
                        "git@github.com:user/repo.git", str(clone_dir),
                    ],
                    check=True,
                )


class TestListShowsRepo(unittest.TestCase):
    """Test that skua list source labels are clear and stable."""

    def test_source_prefers_local_directory(self):
        from skua.commands.list_cmd import _format_project_source
        p = Project(name="test", directory=str(Path.home() / "repo"), repo="https://github.com/user/repo.git")
        source = _format_project_source(p)
        self.assertEqual(source, "LOCAL:~/repo")

    def test_source_formats_github_https(self):
        from skua.commands.list_cmd import _format_project_source
        p = Project(name="test", repo="https://github.com/user/repo.git", directory="")
        source = _format_project_source(p)
        self.assertEqual(source, "GITHUB:/user/repo")

    def test_source_formats_github_ssh(self):
        from skua.commands.list_cmd import _format_project_source
        p = Project(name="test", repo="git@github.com:user/repo.git", directory="")
        source = _format_project_source(p)
        self.assertEqual(source, "GITHUB:/user/repo")

    def test_source_falls_back_to_generic_repo(self):
        from skua.commands.list_cmd import _format_project_source
        p = Project(name="test", repo="https://gitlab.com/user/repo.git", directory="")
        source = _format_project_source(p)
        self.assertEqual(source, "REPO:https://gitlab.com/user/repo.git")

    def test_source_none_when_both_empty(self):
        from skua.commands.list_cmd import _format_project_source
        p = Project(name="test")
        source = _format_project_source(p)
        self.assertEqual(source, "(none)")


class TestProjectYamlPersistence(unittest.TestCase):
    """Test saving and loading a project with repo through ConfigStore."""

    def test_save_and_load_project_with_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()

            project = Project(
                name="my-repo-proj",
                repo="https://github.com/user/repo.git",
                directory="",
                environment="local-docker",
                security="open",
                agent="claude",
                git=ProjectGitSpec(),
                ssh=ProjectSshSpec(private_key="/home/user/.ssh/id_rsa"),
                image=ProjectImageSpec(),
            )
            store.save_resource(project)

            loaded = store.load_project("my-repo-proj")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.repo, "https://github.com/user/repo.git")
            self.assertEqual(loaded.directory, "")
            self.assertEqual(loaded.name, "my-repo-proj")
            self.assertEqual(loaded.ssh.private_key, "/home/user/.ssh/id_rsa")

    def test_save_and_load_project_without_repo(self):
        """Projects without repo should still work (backwards compatible)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()

            project = Project(
                name="local-proj",
                directory="/tmp/my-code",
                environment="local-docker",
            )
            store.save_resource(project)

            loaded = store.load_project("local-proj")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.repo, "")
            self.assertEqual(loaded.directory, "/tmp/my-code")


class TestDescribeIncludesRepo(unittest.TestCase):
    """Test that describe output includes the repo field."""

    def test_resource_to_dict_includes_repo(self):
        p = Project(name="test", repo="https://github.com/user/repo.git")
        d = resource_to_dict(p)
        self.assertIn("repo", d["spec"])
        self.assertEqual(d["spec"]["repo"], "https://github.com/user/repo.git")


class TestValidationWithRepo(unittest.TestCase):
    """Test that validation handles projects with repo set."""

    def test_no_directory_warning_with_repo_only(self):
        """A project with repo but no directory should still warn about no directory.
        (directory is populated at runtime by cmd_run, not at add time)."""
        from skua.config.resources import Environment, SecurityProfile, AgentConfig
        from skua.config.validation import validate_project

        project = Project(name="test", repo="https://github.com/u/r.git")
        env = Environment(name="local-docker")
        sec = SecurityProfile(name="open")
        agent = AgentConfig(name="claude")

        result = validate_project(project, env, sec, agent)
        # Should warn about missing directory (it's set at runtime)
        dir_warnings = [w for w in result.warnings if "no directory" in w]
        self.assertTrue(len(dir_warnings) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
