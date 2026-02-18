# SPDX-License-Identifier: BUSL-1.1
"""YAML resource file discovery, loading, and saving."""

import shutil
from pathlib import Path
from typing import Optional

import yaml

from skua.config.resources import (
    API_VERSION,
    Environment,
    SecurityProfile,
    AgentConfig,
    Credential,
    Project,
    ProjectGitSpec,
    resource_from_dict,
    resource_to_dict,
)


CONFIG_DIR = Path.home() / ".config" / "skua"

# Subdirectories for each resource kind
KIND_DIRS = {
    "Environment": "environments",
    "SecurityProfile": "security",
    "AgentConfig": "agents",
    "Credential": "credentials",
    "Project": "projects",
}


class ConfigStore:
    """Manages YAML resource files on disk.

    Layout:
        ~/.config/skua/
        ├── global.yaml              # global defaults (git identity, default refs)
        ├── environments/            # Environment resources
        ├── security/                # SecurityProfile resources
        ├── agents/                  # AgentConfig resources
        ├── projects/                # Project resources
        ├── claude-data/             # legacy/default persistence (bind mode)
        └── agent-data/              # non-Claude persistence (bind mode)
    """

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or CONFIG_DIR
        self.global_file = self.config_dir / "global.yaml"
        self._global_cache = None

    def ensure_dirs(self):
        """Create config directory structure."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        for subdir in KIND_DIRS.values():
            (self.config_dir / subdir).mkdir(exist_ok=True)

    # ── Global config ────────────────────────────────────────────────

    def load_global(self) -> dict:
        """Load global.yaml (git identity, default refs)."""
        if self._global_cache is not None:
            return self._global_cache
        if self.global_file.exists():
            with open(self.global_file) as f:
                self._global_cache = yaml.safe_load(f) or {}
        else:
            self._global_cache = {}
        return self._global_cache

    def save_global(self, data: dict):
        """Write global.yaml."""
        self.ensure_dirs()
        with open(self.global_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        self._global_cache = data

    def get_global_defaults(self) -> dict:
        """Return the defaults section of global config."""
        return self.load_global().get("defaults", {})

    # ── Resource CRUD ────────────────────────────────────────────────

    def _resource_dir(self, kind: str) -> Path:
        subdir = KIND_DIRS.get(kind)
        if subdir is None:
            raise ValueError(f"Unknown resource kind: {kind}")
        return self.config_dir / subdir

    def _resource_path(self, kind: str, name: str) -> Path:
        return self._resource_dir(kind) / f"{name}.yaml"

    def save_resource(self, resource):
        """Save a resource to its YAML file."""
        kind = type(resource).__name__
        self.ensure_dirs()
        path = self._resource_path(kind, resource.name)
        data = resource_to_dict(resource)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def load_resource(self, kind: str, name: str):
        """Load a single resource by kind and name. Returns None if not found."""
        path = self._resource_path(kind, name)
        if not path.exists():
            return None
        with open(path) as f:
            data = yaml.safe_load(f)
        if data is None:
            return None
        return resource_from_dict(data)

    def delete_resource(self, kind: str, name: str) -> bool:
        """Delete a resource file. Returns True if it existed."""
        path = self._resource_path(kind, name)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_resources(self, kind: str) -> list:
        """List all resource names of a given kind."""
        d = self._resource_dir(kind)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.yaml"))

    def load_all_resources(self, kind: str) -> list:
        """Load all resources of a given kind."""
        names = self.list_resources(kind)
        resources = []
        for name in names:
            r = self.load_resource(kind, name)
            if r is not None:
                resources.append(r)
        return resources

    # ── Typed accessors ──────────────────────────────────────────────

    def load_environment(self, name: str) -> Optional[Environment]:
        return self.load_resource("Environment", name)

    def load_security(self, name: str) -> Optional[SecurityProfile]:
        return self.load_resource("SecurityProfile", name)

    def load_agent(self, name: str) -> Optional[AgentConfig]:
        return self.load_resource("AgentConfig", name)

    def load_credential(self, name: str) -> Optional[Credential]:
        return self.load_resource("Credential", name)

    def load_project(self, name: str) -> Optional[Project]:
        return self.load_resource("Project", name)

    # ── Resolve project with global defaults ─────────────────────────

    def resolve_project(self, name: str) -> Optional[Project]:
        """Load a project, filling in defaults from global config."""
        project = self.load_project(name)
        if project is None:
            return None
        g = self.load_global()
        defaults = g.get("defaults", {})
        git = g.get("git", {})

        # Fill in git identity from global if not set on project
        if not project.git.name:
            project.git.name = git.get("name", "")
        if not project.git.email:
            project.git.email = git.get("email", "")

        # Fill in SSH key from global if not set
        if not project.ssh.private_key:
            project.ssh.private_key = defaults.get("sshKey", "")

        # Fill in references from global defaults if not set
        if not project.environment:
            project.environment = defaults.get("environment", "local-docker")
        if not project.security:
            project.security = defaults.get("security", "open")
        if not project.agent:
            project.agent = defaults.get("agent", "claude")

        return project

    # ── Presets ───────────────────────────────────────────────────────

    def install_presets(self, preset_dir: Path, overwrite: bool = False):
        """Copy shipped preset YAML files into the config directory.

        Only copies files that don't already exist unless overwrite=True.
        """
        self.ensure_dirs()
        for kind, subdir in KIND_DIRS.items():
            src_dir = preset_dir / subdir
            if not src_dir.exists():
                continue
            dest_dir = self.config_dir / subdir
            for src_file in src_dir.glob("*.yaml"):
                dest_file = dest_dir / src_file.name
                if not dest_file.exists() or overwrite:
                    shutil.copy2(src_file, dest_file)

    # ── Persistence paths ────────────────────────────────────────────

    def project_data_dir(self, project_name: str, agent_name: str = "claude") -> Path:
        """Return the bind-mount persistence directory for a project/agent."""
        if not agent_name or agent_name == "claude":
            return self.config_dir / "claude-data" / project_name
        return self.config_dir / "agent-data" / agent_name / project_name

    def claude_data_dir(self, project_name: str) -> Path:
        """Backward-compatible Claude data path helper."""
        return self.project_data_dir(project_name, "claude")

    def repos_dir(self) -> Path:
        """Return the base directory for cloned repositories."""
        return self.config_dir / "repos"

    def repo_dir(self, project_name: str) -> Path:
        """Return the clone directory for a specific project's repo."""
        return self.repos_dir() / project_name

    # ── Tool directory ───────────────────────────────────────────────

    def get_container_dir(self) -> Optional[Path]:
        """Find the directory containing container build assets (entrypoint.sh).

        Dockerfiles are generated dynamically; this locates the directory
        that ships entrypoint.sh and other container build-time assets.
        """
        g = self.load_global()

        # Explicit override from global config
        tool_dir = g.get("toolDir")
        if tool_dir:
            p = Path(tool_dir)
            if (p / "entrypoint.sh").exists():
                return p

        # Standard location: skua/container/ next to this package
        pkg_container = Path(__file__).resolve().parent.parent / "container"
        if (pkg_container / "entrypoint.sh").exists():
            return pkg_container

        # Debian package location
        deb_dir = Path("/usr/lib/skua/skua/container")
        if (deb_dir / "entrypoint.sh").exists():
            return deb_dir

        # Legacy: check repo root (pre-refactor layout)
        repo_root = Path(__file__).resolve().parent.parent.parent
        if (repo_root / "entrypoint.sh").exists():
            return repo_root

        return None

    def is_initialized(self) -> bool:
        """Check if the new YAML config has been set up."""
        return self.global_file.exists()
