import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.docker import _latest_npm_package_version


class TestAgentVersionLookup(unittest.TestCase):
    @mock.patch("skua.docker.urlopen")
    @mock.patch("skua.docker.subprocess.run")
    def test_latest_npm_package_version_falls_back_to_registry_when_npm_missing(self, mock_run, mock_urlopen):
        mock_run.side_effect = FileNotFoundError()
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps({"version": "1.2.3"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        version = _latest_npm_package_version("@openai/codex")

        self.assertEqual("1.2.3", version)
        mock_urlopen.assert_called_once_with("https://registry.npmjs.org/%40openai%2Fcodex/latest", timeout=8)

    @mock.patch("skua.docker.urlopen")
    @mock.patch("skua.docker.subprocess.run")
    def test_latest_npm_package_version_prefers_local_npm_when_available(self, mock_run, mock_urlopen):
        mock_run.return_value = mock.Mock(returncode=0, stdout="2.3.4\n")

        version = _latest_npm_package_version("@openai/codex")

        self.assertEqual("2.3.4", version)
        mock_urlopen.assert_not_called()
