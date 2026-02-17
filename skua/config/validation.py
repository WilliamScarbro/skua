# SPDX-License-Identifier: BUSL-1.1
"""Validation engine for configuration consistency.

Enforces two types of rules:
1. SecurityProfile internal consistency (e.g., verified installs require sudo: false)
2. SecurityProfile → Environment capability requirements
"""


class ValidationError(Exception):
    """Raised when configuration validation fails."""
    def __init__(self, errors: list, warnings: list = None):
        self.errors = errors
        self.warnings = warnings or []
        msg = "; ".join(errors)
        super().__init__(msg)


class ValidationResult:
    """Collects errors and warnings from validation."""
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def raise_if_invalid(self):
        if not self.valid:
            raise ValidationError(self.errors, self.warnings)


def validate_security_internal(security) -> ValidationResult:
    """Check internal consistency of a SecurityProfile."""
    result = ValidationResult()
    s = security

    # Install mode vs sudo
    if s.install.mode == "verified" and s.agent.sudo:
        result.error(
            "install.mode 'verified' requires agent.sudo to be false — "
            "agent could bypass proxy with sudo"
        )
    if s.install.mode == "none" and s.agent.sudo:
        result.warn(
            "install.mode 'none' with agent.sudo true — "
            "agent can still install packages directly via sudo"
        )
    if s.install.mode in ("advisory", "unrestricted") and not s.agent.sudo:
        result.error(
            f"install.mode '{s.install.mode}' requires agent.sudo to be true — "
            "agent needs sudo to install packages"
        )

    # Proxy network vs sudo
    if s.network.outbound == "proxy" and s.agent.sudo:
        result.warn(
            "network.outbound 'proxy' with agent.sudo true — "
            "agent could bypass proxy via raw sockets or iptables changes"
        )

    # Trusted audit requires proxy
    if s.audit.mode == "trusted" and s.network.outbound != "proxy":
        result.error(
            "audit.mode 'trusted' requires network.outbound 'proxy' — "
            "trusted audit requires proxy mediation"
        )

    # Proxy log source requires trusted audit
    if s.image_updates.source == "proxy" and s.audit.mode != "trusted":
        result.error(
            "imageUpdates.source 'proxy' requires audit.mode 'trusted'"
        )

    # Image updates from audit need some audit mode
    if s.image_updates.mode != "disabled" and s.audit.mode == "none":
        result.warn(
            f"imageUpdates.mode '{s.image_updates.mode}' with audit.mode 'none' — "
            "no install data will be available for image updates"
        )

    return result


def validate_environment_internal(environment) -> ValidationResult:
    """Check internal consistency of an Environment."""
    result = ValidationResult()
    env = environment

    # Managed mode requires compose or kubernetes (need a sidecar)
    if env.mode == "managed" and env.driver == "docker":
        result.error(
            "mode 'managed' requires driver 'compose' or 'kubernetes' — "
            "the skua sidecar needs multi-container orchestration. "
            "Use driver 'compose' for local managed mode."
        )

    # gVisor only works with docker or compose drivers
    if (env.docker.container_runtime
            and env.driver not in ("docker", "compose")):
        result.warn(
            f"container_runtime '{env.docker.container_runtime}' is ignored "
            f"for driver '{env.driver}' — gVisor/kata apply to Docker containers only."
        )

    # Unmanaged mode with internal network on plain docker = network=none
    if (env.mode == "unmanaged" and env.driver == "docker"
            and env.network.mode == "internal"):
        result.warn(
            "driver 'docker' with network.mode 'internal' behaves as network=none "
            "(true internal networks require compose). "
            "Use network.mode 'none' to be explicit, or switch to driver 'compose'."
        )

    return result


def validate_security_environment(security, environment) -> ValidationResult:
    """Check that an Environment provides the capabilities a SecurityProfile requires."""
    result = ValidationResult()
    required = security.required_capabilities()
    provided = environment.capabilities()
    missing = required - provided

    for cap in sorted(missing):
        hint = _capability_hint(cap, environment)
        result.error(
            f"security '{security.name}' requires capability '{cap}', "
            f"but environment '{environment.name}' "
            f"(mode: {environment.mode}, driver: {environment.driver}, "
            f"network: {environment.network.mode}) "
            f"does not provide it.{hint}"
        )

    return result


def validate_agent_security(agent, security) -> ValidationResult:
    """Check agent requirements against security profile."""
    result = ValidationResult()

    # Agent login typically needs network
    if agent.auth.login_command and security.network.outbound == "none":
        result.warn(
            f"agent '{agent.name}' loginCommand '{agent.auth.login_command}' "
            f"requires network access, but security '{security.name}' has "
            f"network.outbound 'none'. Pre-authenticate before running."
        )

    return result


def validate_project(project, environment, security, agent) -> ValidationResult:
    """Full validation of a project configuration.

    Runs all consistency checks between the referenced resources.
    """
    result = ValidationResult()

    # Step 1: Environment internal consistency
    env_int_result = validate_environment_internal(environment)
    result.errors.extend(env_int_result.errors)
    result.warnings.extend(env_int_result.warnings)

    # Step 2: SecurityProfile internal consistency
    sec_result = validate_security_internal(security)
    result.errors.extend(sec_result.errors)
    result.warnings.extend(sec_result.warnings)

    # Step 3: SecurityProfile → Environment capabilities
    env_result = validate_security_environment(security, environment)
    result.errors.extend(env_result.errors)
    result.warnings.extend(env_result.warnings)

    # Step 4: Agent ↔ Security compatibility
    agent_result = validate_agent_security(agent, security)
    result.errors.extend(agent_result.errors)
    result.warnings.extend(agent_result.warnings)

    # Step 5: Project-level checks
    if not project.directory:
        result.warn("project has no directory set")

    return result


def _capability_hint(cap: str, env) -> str:
    """Provide a helpful hint for how to get a missing capability."""
    hints = {
        "trusted.proxy": (
            " Switch to mode 'managed' (requires driver 'compose' or 'kubernetes')."
        ),
        "trusted.log": (
            " Switch to mode 'managed' (requires driver 'compose' or 'kubernetes')."
        ),
        "trusted.mcp": (
            " Switch to mode 'managed' for trusted MCP endpoints."
        ),
        "network.internet": (
            " Change network.mode to 'bridge' or 'host'."
        ),
        "network.isolation": (
            " Change network.mode to 'bridge', 'internal', or 'none'."
        ),
        "network.internal": (
            " Change network.mode to 'internal' or 'none'."
        ),
        "sidecar": (
            " Switch to mode 'managed' (requires driver 'compose' or 'kubernetes')."
        ),
        "audit.docker-diff": (
            " Set cleanup to 'persistent' (non-ephemeral containers)."
        ),
    }
    return hints.get(cap, "")
