# SPDX-License-Identifier: BUSL-1.1
"""Resource dataclasses for skua configuration.

Each resource type corresponds to a Kubernetes-style YAML file with
apiVersion, kind, metadata, and spec fields.
"""

from dataclasses import dataclass, field
from typing import Optional


# ── Environment ──────────────────────────────────────────────────────────

@dataclass
class DockerDriverSpec:
    runtime: str = "local"          # local | remote
    remote_host: str = ""           # ssh://user@host (when runtime=remote)
    cleanup: str = "ephemeral"      # ephemeral | persistent
    container_runtime: str = ""     # "" (default/runc) | runsc (gVisor) | kata


@dataclass
class ComposeDriverSpec:
    runtime: str = "local"
    cleanup: str = "ephemeral"


@dataclass
class KubernetesDriverSpec:
    context: str = ""
    namespace: str = "skua"
    storage_class: str = "standard"


@dataclass
class PersistenceSpec:
    mode: str = "bind"              # bind | volume
    base_path: str = "~/.config/skua/claude-data"
    volume_prefix: str = "skua"


@dataclass
class NetworkSpec:
    mode: str = "bridge"            # none | bridge | internal | host


@dataclass
class Environment:
    """Describes where and how containers run.

    mode: unmanaged | managed
        unmanaged — single agent container, skua launches and exits (execvp).
            Advisory-only monitoring, internal MCP (agent can tamper).
        managed — skua runs as a sidecar container alongside the agent.
            Trusted MCP endpoints, verified monitoring, proxy-mediated network.
    """
    name: str = ""
    mode: str = "unmanaged"         # unmanaged | managed
    driver: str = "docker"          # docker | compose | kubernetes
    docker: DockerDriverSpec = field(default_factory=DockerDriverSpec)
    compose: ComposeDriverSpec = field(default_factory=ComposeDriverSpec)
    kubernetes: KubernetesDriverSpec = field(default_factory=KubernetesDriverSpec)
    persistence: PersistenceSpec = field(default_factory=PersistenceSpec)
    network: NetworkSpec = field(default_factory=NetworkSpec)

    def capabilities(self) -> set:
        """Return the set of capabilities this environment provides."""
        caps = set()

        # Network capabilities
        if self.network.mode == "bridge":
            caps.add("network.internet")
            caps.add("network.isolation")
        elif self.network.mode == "internal":
            caps.add("network.isolation")
            caps.add("network.internal")
        elif self.network.mode == "none":
            caps.add("network.isolation")
            caps.add("network.internal")
        elif self.network.mode == "host":
            caps.add("network.internet")

        # Managed mode provides sidecar/proxy/log capabilities
        # (skua sidecar container handles these)
        if self.mode == "managed":
            caps.add("sidecar")
            caps.add("trusted.proxy")
            caps.add("trusted.log")
            caps.add("trusted.mcp")

        # Container privilege is always available (image-level choice)
        caps.add("container.sudo")
        caps.add("container.no-sudo")

        # gVisor isolation
        if (self.driver in ("docker", "compose")
                and self.docker.container_runtime == "runsc"):
            caps.add("isolation.gvisor")

        # Docker diff requires persistent cleanup
        cleanup = (
            self.docker.cleanup if self.driver == "docker"
            else self.compose.cleanup if self.driver == "compose"
            else ""
        )
        if cleanup == "persistent" and self.driver in ("docker", "compose"):
            caps.add("audit.docker-diff")

        return caps


# ── SecurityProfile ──────────────────────────────────────────────────────

@dataclass
class ProxySpec:
    allowed_domains: list = field(default_factory=list)
    log_requests: bool = True


@dataclass
class SecurityNetworkSpec:
    outbound: str = "unrestricted"  # unrestricted | none | proxy
    proxy: ProxySpec = field(default_factory=ProxySpec)


@dataclass
class VerifiedInstallSpec:
    auto_approve: list = field(default_factory=list)


@dataclass
class SecurityInstallSpec:
    mode: str = "none"              # unrestricted | advisory | verified | none
    verified: VerifiedInstallSpec = field(default_factory=VerifiedInstallSpec)


@dataclass
class SecurityAuditSpec:
    mode: str = "none"              # none | advisory | trusted


@dataclass
class ImageUpdatesSpec:
    mode: str = "disabled"          # disabled | suggest | auto
    source: str = "audit"           # audit | proxy


@dataclass
class SecurityAgentSpec:
    sudo: bool = False


@dataclass
class SecurityProfile:
    """Declares what the agent is and isn't allowed to do."""
    name: str = ""
    network: SecurityNetworkSpec = field(default_factory=SecurityNetworkSpec)
    agent: SecurityAgentSpec = field(default_factory=SecurityAgentSpec)
    install: SecurityInstallSpec = field(default_factory=SecurityInstallSpec)
    audit: SecurityAuditSpec = field(default_factory=SecurityAuditSpec)
    image_updates: ImageUpdatesSpec = field(default_factory=ImageUpdatesSpec)

    def required_capabilities(self) -> set:
        """Return capabilities this profile requires from an Environment."""
        caps = set()

        if self.network.outbound == "unrestricted":
            caps.add("network.internet")
        elif self.network.outbound == "none":
            caps.add("network.isolation")
        elif self.network.outbound == "proxy":
            caps.add("trusted.proxy")
            caps.add("network.internal")

        if self.audit.mode == "trusted":
            caps.add("trusted.log")

        if self.install.mode == "verified":
            caps.add("trusted.proxy")

        if self.image_updates.source == "proxy":
            caps.add("trusted.log")

        return caps


# ── AgentConfig ──────────────────────────────────────────────────────────

@dataclass
class AgentInstallSpec:
    commands: list = field(default_factory=list)
    required_packages: list = field(default_factory=list)
    base_image: str = ""


@dataclass
class AgentRuntimeSpec:
    command: str = ""
    env: dict = field(default_factory=dict)
    entrypoint_hooks: list = field(default_factory=list)


@dataclass
class AgentAuthSpec:
    dir: str = ""                   # directory mounted for persistence
    files: list = field(default_factory=list)
    login_command: str = ""


@dataclass
class AgentConfig:
    """Describes an AI agent: install, auth, runtime."""
    name: str = ""
    install: AgentInstallSpec = field(default_factory=AgentInstallSpec)
    runtime: AgentRuntimeSpec = field(default_factory=AgentRuntimeSpec)
    auth: AgentAuthSpec = field(default_factory=AgentAuthSpec)


# ── Credential ───────────────────────────────────────────────────────────

@dataclass
class Credential:
    """Named credential set for an agent, pointing to host credential files."""
    name: str = ""
    agent: str = "claude"       # references AgentConfig by name
    source_dir: str = ""        # host directory containing auth files
    files: list = field(default_factory=list)   # explicit file paths (takes priority over source_dir)


# ── Project ──────────────────────────────────────────────────────────────

@dataclass
class ProjectGitSpec:
    name: str = ""
    email: str = ""


@dataclass
class ProjectSshSpec:
    private_key: str = ""


@dataclass
class ProjectImageSpec:
    base_image: str = ""
    from_image: str = ""
    extra_packages: list = field(default_factory=list)
    extra_commands: list = field(default_factory=list)
    version: int = 0


@dataclass
class Project:
    """Ties Environment, SecurityProfile, and AgentConfig together for a codebase."""
    name: str = ""
    directory: str = ""
    repo: str = ""                            # git URL (cloned to managed dir)
    environment: str = "local-docker"     # references Environment by name
    security: str = "open"                # references SecurityProfile by name
    agent: str = "claude"                 # references AgentConfig by name
    credential: str = ""                  # references Credential by name (optional)
    git: ProjectGitSpec = field(default_factory=ProjectGitSpec)
    ssh: ProjectSshSpec = field(default_factory=ProjectSshSpec)
    image: ProjectImageSpec = field(default_factory=ProjectImageSpec)


# ── Serialization helpers ────────────────────────────────────────────────

API_VERSION = "skua/v1"

KIND_MAP = {
    "Environment": Environment,
    "SecurityProfile": SecurityProfile,
    "AgentConfig": AgentConfig,
    "Credential": Credential,
    "Project": Project,
}


def resource_to_dict(resource) -> dict:
    """Convert a resource dataclass to a YAML-serializable dict."""
    kind = type(resource).__name__

    spec = _dataclass_to_dict(resource)
    name = spec.pop("name", "")

    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": name},
        "spec": spec,
    }


def resource_from_dict(data: dict):
    """Parse a YAML dict into the appropriate resource dataclass."""
    kind = data.get("kind", "")
    cls = KIND_MAP.get(kind)
    if cls is None:
        raise ValueError(f"Unknown resource kind: {kind}")

    name = data.get("metadata", {}).get("name", "")
    spec = data.get("spec", {})
    spec["name"] = name

    return _dict_to_dataclass(cls, spec)


def _dataclass_to_dict(obj) -> dict:
    """Recursively convert a dataclass to a plain dict."""
    from dataclasses import fields, is_dataclass
    if not is_dataclass(obj):
        return obj
    result = {}
    for f in fields(obj):
        val = getattr(obj, f.name)
        if is_dataclass(val):
            val = _dataclass_to_dict(val)
        elif isinstance(val, list):
            val = [_dataclass_to_dict(v) if is_dataclass(v) else v for v in val]
        elif isinstance(val, dict):
            val = {k: _dataclass_to_dict(v) if is_dataclass(v) else v for k, v in val.items()}
        result[f.name] = val
    return result


def _dict_to_dataclass(cls, data: dict):
    """Recursively construct a dataclass from a dict, using snake_case field matching."""
    from dataclasses import fields, is_dataclass

    if not isinstance(data, dict):
        return data

    kwargs = {}
    field_map = {f.name: f for f in fields(cls)}

    # Also build a camelCase → snake_case lookup
    alias_map = {}
    for f in fields(cls):
        # Convert snake_case field name to camelCase for YAML compatibility
        parts = f.name.split("_")
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        alias_map[camel] = f.name
        alias_map[f.name] = f.name

    for key, val in data.items():
        field_name = alias_map.get(key, key)
        if field_name not in field_map:
            continue
        f = field_map[field_name]
        field_type = f.type

        # Resolve string type annotations
        if isinstance(field_type, str):
            field_type = eval(field_type)

        # Handle Optional types
        origin = getattr(field_type, "__origin__", None)
        if origin is not None:
            # For list, dict, etc. just pass through
            kwargs[field_name] = val
        elif is_dataclass(field_type):
            kwargs[field_name] = _dict_to_dataclass(field_type, val) if isinstance(val, dict) else val
        else:
            kwargs[field_name] = val

    return cls(**kwargs)
