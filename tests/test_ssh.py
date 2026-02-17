#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
"""Tests for SSH key integration in skua containers.

Validates that SSH keys are properly mounted, permissions are correct,
and git operations over SSH work inside the container.

Usage:
    python3 test_ssh.py --ssh-key ~/.ssh/id_rsa --repo git@github.com:User/repo.git

Environment variables (alternative to flags):
    SKUA_TEST_SSH_KEY   - Path to SSH private key
    SKUA_TEST_REPO      - SSH git repo URL to test cloning
"""

import argparse
import os
import subprocess
import sys
import unittest
from pathlib import Path

# ── Resolve test parameters from flags or env ─────────────────────────────

SSH_KEY = None
REPO_URL = None
IMAGE_NAME = "skua-base"
CONTAINER_PREFIX = "skua-test"


def parse_test_args():
    """Parse --ssh-key and --repo before unittest takes over."""
    global SSH_KEY, REPO_URL
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ssh-key", default=os.environ.get("SKUA_TEST_SSH_KEY", ""))
    parser.add_argument("--repo", default=os.environ.get("SKUA_TEST_REPO", ""))
    args, remaining = parser.parse_known_args()
    SSH_KEY = args.ssh_key
    REPO_URL = args.repo
    # Return remaining args so unittest can parse them
    return remaining


def require_ssh_key():
    if not SSH_KEY or not Path(SSH_KEY).is_file():
        raise unittest.SkipTest(
            f"SSH key not found: {SSH_KEY!r}. "
            "Pass --ssh-key or set SKUA_TEST_SSH_KEY."
        )


def require_repo():
    if not REPO_URL:
        raise unittest.SkipTest(
            "No repo URL provided. Pass --repo or set SKUA_TEST_REPO."
        )


def docker_run(cmd, mounts=None, env=None, timeout=60):
    """Run a command inside a fresh skua container and return stdout.

    Uses a marker line to separate entrypoint banner output from the
    actual command output, so tests can reliably parse results.
    """
    marker = "___SKUA_TEST_OUTPUT___"
    wrapped_cmd = f'echo "{marker}"; {cmd}'
    docker_cmd = [
        "docker", "run", "--rm",
        "--name", f"{CONTAINER_PREFIX}-{os.getpid()}",
    ]
    for src, dst, mode in (mounts or []):
        docker_cmd.extend(["-v", f"{src}:{dst}:{mode}"])
    for k, v in (env or {}).items():
        docker_cmd.extend(["-e", f"{k}={v}"])
    docker_cmd.extend([IMAGE_NAME, "bash", "-c", wrapped_cmd])

    result = subprocess.run(
        docker_cmd, capture_output=True, text=True, timeout=timeout
    )
    # Strip entrypoint banner: everything before the marker
    if marker in result.stdout:
        result.stdout = result.stdout.split(marker, 1)[1].lstrip("\n")
    return result


def image_exists():
    result = subprocess.run(
        ["docker", "image", "inspect", IMAGE_NAME],
        capture_output=True, text=True
    )
    return result.returncode == 0


# ── Tests ──────────────────────────────────────────────────────────────────


class TestSSHKeyMounting(unittest.TestCase):
    """Test that SSH keys are correctly mounted into the container."""

    @classmethod
    def setUpClass(cls):
        if not image_exists():
            raise unittest.SkipTest(f"Docker image '{IMAGE_NAME}' not found. Run 'skua build' first.")
        require_ssh_key()

    def _ssh_mounts(self):
        """Build the standard SSH mount list matching skua's behavior."""
        key_path = Path(SSH_KEY).resolve()
        key_name = key_path.name
        mounts = [(str(key_path), f"/home/dev/.ssh-mount/{key_name}", "ro")]
        pub = Path(f"{key_path}.pub")
        if pub.is_file():
            mounts.append((str(pub), f"/home/dev/.ssh-mount/{key_name}.pub", "ro"))
        known = key_path.parent / "known_hosts"
        if known.is_file():
            mounts.append((str(known), "/home/dev/.ssh-mount/known_hosts", "ro"))
        return mounts

    def test_key_is_mounted(self):
        """Private key file exists inside the container."""
        key_name = Path(SSH_KEY).name
        result = docker_run(
            f"test -f /home/dev/.ssh-mount/{key_name} && echo OK",
            mounts=self._ssh_mounts(),
        )
        self.assertEqual(result.stdout.strip(), "OK", result.stderr)

    def test_pub_key_is_mounted(self):
        """Public key file exists if available on host."""
        pub = Path(f"{SSH_KEY}.pub")
        if not pub.is_file():
            self.skipTest("No .pub file for this key")
        key_name = Path(SSH_KEY).name
        result = docker_run(
            f"test -f /home/dev/.ssh-mount/{key_name}.pub && echo OK",
            mounts=self._ssh_mounts(),
        )
        self.assertEqual(result.stdout.strip(), "OK", result.stderr)

    def test_known_hosts_is_mounted(self):
        """known_hosts file exists if available on host."""
        known = Path(SSH_KEY).parent / "known_hosts"
        if not known.is_file():
            self.skipTest("No known_hosts file")
        result = docker_run(
            "test -f /home/dev/.ssh-mount/known_hosts && echo OK",
            mounts=self._ssh_mounts(),
        )
        self.assertEqual(result.stdout.strip(), "OK", result.stderr)

    def test_entrypoint_copies_key_with_correct_permissions(self):
        """Entrypoint copies keys to ~/.ssh with 600 permissions."""
        key_name = Path(SSH_KEY).name
        result = docker_run(
            f'stat -c "%a" /home/dev/.ssh/{key_name}',
            mounts=self._ssh_mounts(),
        )
        self.assertEqual(result.stdout.strip(), "600",
                         f"Expected 600 permissions, got: {result.stdout.strip()}\n{result.stderr}")

    def test_ssh_dir_permissions(self):
        """~/.ssh directory has 700 permissions after entrypoint."""
        result = docker_run(
            'stat -c "%a" /home/dev/.ssh',
            mounts=self._ssh_mounts(),
        )
        self.assertEqual(result.stdout.strip(), "700",
                         f"Expected 700 permissions, got: {result.stdout.strip()}\n{result.stderr}")

    def test_git_ssh_command_is_set(self):
        """GIT_SSH_COMMAND is configured with the key after entrypoint."""
        key_name = Path(SSH_KEY).name
        result = docker_run(
            'echo "$GIT_SSH_COMMAND"',
            mounts=self._ssh_mounts(),
        )
        self.assertIn(key_name, result.stdout,
                      f"GIT_SSH_COMMAND should reference {key_name}: {result.stdout}")
        self.assertIn("-i", result.stdout)


class TestSSHGitOperations(unittest.TestCase):
    """Test that git operations over SSH work inside the container."""

    @classmethod
    def setUpClass(cls):
        if not image_exists():
            raise unittest.SkipTest(f"Docker image '{IMAGE_NAME}' not found. Run 'skua build' first.")
        require_ssh_key()
        require_repo()

    def _ssh_mounts(self):
        key_path = Path(SSH_KEY).resolve()
        key_name = key_path.name
        mounts = [(str(key_path), f"/home/dev/.ssh-mount/{key_name}", "ro")]
        pub = Path(f"{key_path}.pub")
        if pub.is_file():
            mounts.append((str(pub), f"/home/dev/.ssh-mount/{key_name}.pub", "ro"))
        known = key_path.parent / "known_hosts"
        if known.is_file():
            mounts.append((str(known), "/home/dev/.ssh-mount/known_hosts", "ro"))
        return mounts

    def test_ssh_github_auth(self):
        """SSH authentication to the git host succeeds."""
        # Extract user@host from repo URL (git@github.com:user/repo.git -> git@github.com)
        if "@" not in REPO_URL:
            self.skipTest("Cannot extract host from repo URL")
        user_host = REPO_URL.split(":")[0]  # git@github.com
        result = docker_run(
            f'ssh -T -o StrictHostKeyChecking=accept-new {user_host} 2>&1; true',
            mounts=self._ssh_mounts(),
            timeout=30,
        )
        # GitHub returns exit 1 but prints "successfully authenticated"
        output = result.stdout + result.stderr
        self.assertTrue(
            "successfully authenticated" in output.lower()
            or "welcome" in output.lower(),
            f"SSH auth to {user_host} failed. Output:\n{output}"
        )

    def test_git_clone(self):
        """git clone over SSH succeeds inside the container."""
        result = docker_run(
            f'git clone --depth 1 {REPO_URL} /tmp/test-clone '
            f'&& test -d /tmp/test-clone/.git && echo OK',
            mounts=self._ssh_mounts(),
            env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com"},
            timeout=60,
        )
        self.assertIn("OK", result.stdout,
                       f"Clone failed.\nstdout: {result.stdout}\nstderr: {result.stderr}")

    def test_git_ls_remote(self):
        """git ls-remote over SSH succeeds (lighter than clone)."""
        result = docker_run(
            f'git ls-remote --heads {REPO_URL} 2>&1 | head -5',
            mounts=self._ssh_mounts(),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         f"ls-remote failed.\nstdout: {result.stdout}\nstderr: {result.stderr}")
        # Should contain at least one ref
        self.assertTrue(
            "refs/heads" in result.stdout,
            f"No refs found in output: {result.stdout}"
        )


class TestNoSSHKey(unittest.TestCase):
    """Test container behavior when no SSH key is provided."""

    @classmethod
    def setUpClass(cls):
        if not image_exists():
            raise unittest.SkipTest(f"Docker image '{IMAGE_NAME}' not found. Run 'skua build' first.")

    def test_no_ssh_mount_dir(self):
        """Without SSH mounts, .ssh-mount is empty or absent and entrypoint handles it."""
        result = docker_run(
            'if [ -d /home/dev/.ssh-mount ]; then '
            '  count=$(ls -A /home/dev/.ssh-mount 2>/dev/null | wc -l); '
            '  echo "exists:empty=$([[ $count -eq 0 ]] && echo yes || echo no)"; '
            'else '
            '  echo "not-found"; '
            'fi',
        )
        output = result.stdout.strip()
        # Either the dir doesn't exist or it exists but is empty
        self.assertTrue(
            output == "not-found" or "empty=yes" in output,
            f"Expected empty or missing .ssh-mount, got: {output}"
        )

    def test_git_ssh_command_not_set(self):
        """GIT_SSH_COMMAND is not set when no key is mounted."""
        result = docker_run(
            'echo "GIT_SSH_COMMAND=${GIT_SSH_COMMAND:-UNSET}"',
        )
        self.assertIn("UNSET", result.stdout)


if __name__ == "__main__":
    remaining = parse_test_args()
    # Pass remaining args to unittest
    unittest.main(argv=[sys.argv[0]] + remaining, verbosity=2)
