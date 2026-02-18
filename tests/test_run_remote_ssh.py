#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
"""Tests for remote-Docker SSH preflight checks in `skua run`."""

import unittest
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skua.config.resources import Project


class TestRemoteDockerSshPreflight(unittest.TestCase):
    """Validate local SSH preflight behavior for remote Docker hosts."""

    def setUp(self):
        self._orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_missing_ssh_binary_exits(self):
        from skua.commands.run import _ensure_local_ssh_client_for_remote_docker

        with mock.patch("skua.commands.run.shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                _ensure_local_ssh_client_for_remote_docker("docker.example.com")
            self.assertEqual(ctx.exception.code, 1)

    def test_non_executable_ssh_binary_exits(self):
        from skua.commands.run import _ensure_local_ssh_client_for_remote_docker

        with mock.patch("skua.commands.run.shutil.which", return_value="/usr/bin/ssh"):
            with mock.patch("skua.commands.run.os.access", return_value=False):
                with self.assertRaises(SystemExit) as ctx:
                    _ensure_local_ssh_client_for_remote_docker("docker.example.com")
                self.assertEqual(ctx.exception.code, 1)

    def test_permission_denied_on_ssh_exec_exits(self):
        from skua.commands.run import _ensure_local_ssh_client_for_remote_docker

        with mock.patch("skua.commands.run.shutil.which", return_value="/usr/bin/ssh"):
            with mock.patch("skua.commands.run.os.access", return_value=True):
                with mock.patch("skua.commands.run.subprocess.run", side_effect=PermissionError):
                    with self.assertRaises(SystemExit) as ctx:
                        _ensure_local_ssh_client_for_remote_docker("docker.example.com")
                    self.assertEqual(ctx.exception.code, 1)

    def test_healthy_ssh_binary_passes(self):
        from skua.commands.run import _ensure_local_ssh_client_for_remote_docker

        with mock.patch("skua.commands.run.shutil.which", return_value="/usr/bin/ssh"):
            with mock.patch("skua.commands.run.os.access", return_value=True):
                with mock.patch("skua.commands.run.subprocess.run") as mock_run:
                    _ensure_local_ssh_client_for_remote_docker("docker.example.com")
                    mock_run.assert_called_once_with(
                        ["/usr/bin/ssh", "-V"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )

    def test_cmd_run_invokes_preflight_for_remote_host(self):
        from skua.commands.run import cmd_run

        fake_project = Project(name="qar", host="docker.example.com")

        with mock.patch("skua.commands.run.ConfigStore") as MockStore:
            store = MockStore.return_value
            store.resolve_project.return_value = fake_project

            with mock.patch("skua.commands.run._ensure_local_ssh_client_for_remote_docker") as mock_preflight:
                with mock.patch("skua.commands.run._configure_remote_docker_transport"):
                    with mock.patch("skua.commands.run.is_container_running", return_value=True):
                        with mock.patch("builtins.input", return_value="n"):
                            cmd_run(SimpleNamespace(name="qar"))
                            mock_preflight.assert_called_once_with("docker.example.com")


class TestRemoteDockerTransportFallback(unittest.TestCase):
    """Validate remote transport fallback sequence for `skua run`."""

    def setUp(self):
        self._orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_configure_transport_keeps_docker_host_when_probe_succeeds(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch("skua.commands.run._probe_current_docker_connection", return_value=(True, "")):
                with mock.patch("skua.commands.run._enable_ssh_docker_wrapper") as mock_wrapper:
                    _configure_remote_docker_transport("docker.example.com")
                    self.assertEqual(
                        "ssh://docker.example.com",
                        os.environ.get("DOCKER_HOST", ""),
                    )
                    mock_wrapper.assert_not_called()

    def test_configure_transport_exits_when_user_cancels(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch(
                "skua.commands.run._probe_current_docker_connection",
                return_value=(False, "permission denied"),
            ):
                with mock.patch("skua.commands.run._prompt_remote_docker_recovery_action", return_value="cancel"):
                    with mock.patch("sys.stdin.isatty", return_value=True):
                        with mock.patch("sys.stdout.isatty", return_value=True):
                            with self.assertRaises(SystemExit) as ctx:
                                _configure_remote_docker_transport("docker.example.com")
                            self.assertEqual(ctx.exception.code, 1)

    def test_configure_transport_falls_back_when_user_selects_fallback(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch(
                "skua.commands.run._probe_current_docker_connection",
                side_effect=[(False, "permission denied"), (True, "")],
            ):
                with mock.patch("skua.commands.run._prompt_remote_docker_recovery_action", return_value="fallback"):
                    with mock.patch("sys.stdin.isatty", return_value=True):
                        with mock.patch("sys.stdout.isatty", return_value=True):
                            with mock.patch("skua.commands.run._enable_ssh_docker_wrapper") as mock_wrapper:
                                _configure_remote_docker_transport("docker.example.com")
                                mock_wrapper.assert_called_once_with("docker.example.com")

    def test_configure_transport_install_success_retries_and_returns(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch(
                "skua.commands.run._probe_current_docker_connection",
                side_effect=[(False, "permission denied"), (True, "")],
            ):
                with mock.patch("skua.commands.run._prompt_remote_docker_recovery_action", return_value="install"):
                    with mock.patch("skua.commands.run._run_docker_cli_installer", return_value=True):
                        with mock.patch("sys.stdin.isatty", return_value=True):
                            with mock.patch("sys.stdout.isatty", return_value=True):
                                with mock.patch("skua.commands.run._enable_ssh_docker_wrapper") as mock_wrapper:
                                    _configure_remote_docker_transport("docker.example.com")
                                    mock_wrapper.assert_not_called()

    def test_configure_transport_install_fail_then_decline_fallback_exits(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch(
                "skua.commands.run._probe_current_docker_connection",
                return_value=(False, "permission denied"),
            ):
                with mock.patch("skua.commands.run._prompt_remote_docker_recovery_action", return_value="install"):
                    with mock.patch("skua.commands.run._run_docker_cli_installer", return_value=False):
                        with mock.patch("builtins.input", return_value="n"):
                            with mock.patch("sys.stdin.isatty", return_value=True):
                                with mock.patch("sys.stdout.isatty", return_value=True):
                                    with self.assertRaises(SystemExit) as ctx:
                                        _configure_remote_docker_transport("docker.example.com")
                                    self.assertEqual(ctx.exception.code, 1)

    def test_configure_transport_falls_back_when_install_does_not_fix_connection(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch(
                "skua.commands.run._probe_current_docker_connection",
                side_effect=[(False, "permission denied"), (False, "still denied"), (True, "")],
            ):
                with mock.patch("skua.commands.run._prompt_remote_docker_recovery_action", return_value="install"):
                    with mock.patch("skua.commands.run._run_docker_cli_installer", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch("sys.stdin.isatty", return_value=True):
                                with mock.patch("sys.stdout.isatty", return_value=True):
                                    with mock.patch("skua.commands.run._enable_ssh_docker_wrapper") as mock_wrapper:
                                        _configure_remote_docker_transport("docker.example.com")
                                        mock_wrapper.assert_called_once_with("docker.example.com")

    def test_configure_transport_falls_back_when_non_interactive(self):
        from skua.commands.run import _configure_remote_docker_transport

        with mock.patch("skua.commands.run._prefer_non_snap_docker_on_path", return_value=""):
            with mock.patch(
                "skua.commands.run._probe_current_docker_connection",
                side_effect=[(False, "permission denied"), (True, "")],
            ):
                with mock.patch("sys.stdin.isatty", return_value=False):
                    with mock.patch("sys.stdout.isatty", return_value=False):
                        with mock.patch("skua.commands.run._enable_ssh_docker_wrapper") as mock_wrapper:
                            _configure_remote_docker_transport("docker.example.com")
                            mock_wrapper.assert_called_once_with("docker.example.com")

    def test_prompt_remote_docker_recovery_action_maps_choices(self):
        from skua.commands.run import _prompt_remote_docker_recovery_action

        with mock.patch("builtins.input", return_value="1"):
            self.assertEqual("install", _prompt_remote_docker_recovery_action())
        with mock.patch("builtins.input", return_value="2"):
            self.assertEqual("fallback", _prompt_remote_docker_recovery_action())
        with mock.patch("builtins.input", return_value="3"):
            self.assertEqual("cancel", _prompt_remote_docker_recovery_action())
        with mock.patch("builtins.input", return_value="unknown"):
            self.assertEqual("cancel", _prompt_remote_docker_recovery_action())

    def test_is_snap_binary_detects_snap_bin_path(self):
        from skua.commands.run import _is_snap_binary
        self.assertTrue(_is_snap_binary("/snap/bin/docker"))

    def test_find_non_snap_docker_binary_prefers_installed_candidate(self):
        from skua.commands.run import _find_non_snap_docker_binary

        with mock.patch("skua.commands.run.shutil.which", return_value="/snap/bin/docker"):
            with mock.patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
                with mock.patch("skua.commands.run.os.access") as mock_access:
                    # Only /usr/local/bin/docker exists and is executable.
                    mock_is_file.side_effect = lambda p: str(p) == "/usr/local/bin/docker"
                    mock_access.side_effect = lambda p, mode: str(p) == "/usr/local/bin/docker"
                    self.assertEqual("/usr/local/bin/docker", _find_non_snap_docker_binary())

    def test_cmd_run_uses_fallback_path_when_transport_declined(self):
        from skua.commands.run import cmd_run

        fake_project = Project(name="qar", host="docker.example.com")

        with mock.patch("skua.commands.run.ConfigStore") as MockStore:
            store = MockStore.return_value
            store.resolve_project.return_value = fake_project

            with mock.patch("skua.commands.run._ensure_local_ssh_client_for_remote_docker"):
                with mock.patch("skua.commands.run._configure_remote_docker_transport") as mock_transport:
                    with mock.patch("skua.commands.run.is_container_running", return_value=True):
                        with mock.patch("builtins.input", return_value="n"):
                            cmd_run(SimpleNamespace(name="qar"))
                            mock_transport.assert_called_once_with("docker.example.com")


class TestRemoteRepoCloneWithProjectSshKey(unittest.TestCase):
    """Validate remote repo clone behavior with project SSH key support."""

    def setUp(self):
        self._orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_remote_clone_uses_project_ssh_key_and_known_hosts(self):
        from skua.commands.run import _clone_repo_into_remote_volume

        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "id_ed25519")
            known_hosts_path = os.path.join(tmpdir, "known_hosts")
            with open(key_path, "w", encoding="utf-8") as f:
                f.write("-----BEGIN TEST KEY-----\nabc\n-----END TEST KEY-----\n")
            with open(known_hosts_path, "w", encoding="utf-8") as f:
                f.write("github.com ssh-ed25519 AAAA...\n")

            project = Project(name="qar", repo="git@github.com:org/repo.git")
            project.ssh.private_key = key_path

            mock_check = mock.Mock(returncode=0, stdout="empty\n")
            mock_clone = mock.Mock(returncode=0)
            with mock.patch("skua.commands.run.subprocess.run", side_effect=[mock_check, mock_clone]) as mock_run:
                _clone_repo_into_remote_volume(project, "skua-qar-repo")

                self.assertEqual(2, mock_run.call_count)
                clone_call = mock_run.call_args_list[1]
                clone_cmd = clone_call.args[0]
                clone_env = clone_call.kwargs.get("env", {})

                self.assertIn("-e", clone_cmd)
                self.assertIn("SKUA_REMOTE_GIT_REPO", clone_cmd)
                self.assertIn("SKUA_REMOTE_GIT_SSH_KEY_B64", clone_cmd)
                self.assertIn("SKUA_REMOTE_GIT_KNOWN_HOSTS_B64", clone_cmd)
                self.assertIn("--entrypoint", clone_cmd)
                self.assertIn("sh", clone_cmd)
                self.assertIn("alpine/git", clone_cmd)
                self.assertEqual("git@github.com:org/repo.git", clone_env.get("SKUA_REMOTE_GIT_REPO"))
                self.assertTrue(clone_env.get("SKUA_REMOTE_GIT_SSH_KEY_B64"))
                self.assertTrue(clone_env.get("SKUA_REMOTE_GIT_KNOWN_HOSTS_B64"))

    def test_remote_clone_uses_accept_new_even_without_project_key(self):
        from skua.commands.run import _clone_repo_into_remote_volume

        project = Project(name="qar", repo="git@github.com:org/repo.git")
        project.ssh.private_key = ""

        mock_check = mock.Mock(returncode=0, stdout="empty\n")
        mock_clone = mock.Mock(returncode=0)
        with mock.patch("skua.commands.run.subprocess.run", side_effect=[mock_check, mock_clone]) as mock_run:
            _clone_repo_into_remote_volume(project, "skua-qar-repo")
            clone_cmd = mock_run.call_args_list[1].args[0]
            script = clone_cmd[-1]
            self.assertIn("StrictHostKeyChecking=accept-new", script)
            self.assertNotIn("SKUA_REMOTE_GIT_SSH_KEY_B64", clone_cmd)


class TestRemoteAuthSeeding(unittest.TestCase):
    """Validate host-to-remote auth seeding behavior."""

    def test_seed_auth_into_remote_volume_copies_missing_files(self):
        from skua.commands.run import _seed_auth_into_remote_volume

        with tempfile.TemporaryDirectory() as tmpdir:
            auth_file = Path(tmpdir) / "auth.json"
            auth_file.write_text('{"token":"abc"}')

            check_missing = mock.Mock(returncode=1)
            copy_ok = mock.Mock(returncode=0)
            with mock.patch(
                "skua.commands.run.resolve_credential_sources",
                return_value=[(auth_file, "auth.json")],
            ):
                with mock.patch("skua.commands.run.subprocess.run", side_effect=[check_missing, copy_ok]) as mock_run:
                    copied = _seed_auth_into_remote_volume("qar", "claude", cred=None, agent=mock.Mock(), overwrite=False)
                    self.assertEqual(1, copied)
                    self.assertEqual(2, mock_run.call_count)

    def test_seed_auth_into_remote_volume_skips_existing_when_not_overwriting(self):
        from skua.commands.run import _seed_auth_into_remote_volume

        with tempfile.TemporaryDirectory() as tmpdir:
            auth_file = Path(tmpdir) / "auth.json"
            auth_file.write_text('{"token":"abc"}')

            check_exists = mock.Mock(returncode=0)
            with mock.patch(
                "skua.commands.run.resolve_credential_sources",
                return_value=[(auth_file, "auth.json")],
            ):
                with mock.patch("skua.commands.run.subprocess.run", side_effect=[check_exists]) as mock_run:
                    copied = _seed_auth_into_remote_volume("qar", "claude", cred=None, agent=mock.Mock(), overwrite=False)
                    self.assertEqual(0, copied)
                    self.assertEqual(1, mock_run.call_count)

    def test_seed_auth_into_remote_volume_overwrite_skips_existence_check(self):
        from skua.commands.run import _seed_auth_into_remote_volume

        with tempfile.TemporaryDirectory() as tmpdir:
            auth_file = Path(tmpdir) / "auth.json"
            auth_file.write_text('{"token":"abc"}')

            copy_ok = mock.Mock(returncode=0)
            with mock.patch(
                "skua.commands.run.resolve_credential_sources",
                return_value=[(auth_file, "auth.json")],
            ):
                with mock.patch("skua.commands.run.subprocess.run", side_effect=[copy_ok]) as mock_run:
                    copied = _seed_auth_into_remote_volume("qar", "claude", cred=None, agent=mock.Mock(), overwrite=True)
                    self.assertEqual(1, copied)
                    self.assertEqual(1, mock_run.call_count)


if __name__ == "__main__":
    unittest.main()
