<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Security Guide

## Trust Model

Skua's security model is built on one core principle: **the agent has admin inside its container**. Any in-container controls (scripts, wrappers, traps) are advisory — a determined agent can modify or bypass them. Enforceable controls must be external to the agent container.

### Unmanaged vs Managed Mode

Environments have a **mode** that determines the trust boundary:

**Unmanaged** — single container, skua launches and exits. Simple and lightweight.
- All monitoring is advisory (agent can tamper)
- Internal MCP servers are not trusted
- Security relies on Docker-level controls: network isolation, sudo removal

**Managed** — skua runs as a sidecar container alongside the agent.
- Trusted MCP endpoints exposed by the sidecar
- Verified monitoring via the sidecar
- Proxy-mediated network access with domain allowlisting
- Requires `compose` or `kubernetes` driver

## Security Profiles

### `open` — No Restrictions

```yaml
agent.sudo: true
network.outbound: unrestricted
install.mode: unrestricted
audit.mode: none
```

Agent has full sudo, direct internet, can install anything. No monitoring. Use for trusted code in development.

**Requires**: unmanaged or managed, bridge or host network.

### `standard` — Advisory Tracking

```yaml
agent.sudo: true
network.outbound: unrestricted
install.mode: advisory
audit.mode: advisory
imageUpdates.mode: suggest
```

Agent has sudo and internet, but installs are logged (advisory) and the audit system tracks changes via in-container EXIT traps. Image updates are suggested based on audit logs.

**Important**: Advisory tracking is cooperative — the agent could disable the wrappers. This is fine for most development where the agent is acting in good faith.

**Requires**: unmanaged or managed, bridge or host network.

### `hardened` — Proxy-Mediated

```yaml
agent.sudo: false
network.outbound: proxy
install.mode: verified
audit.mode: trusted
imageUpdates.mode: suggest
```

Agent has no sudo and no direct internet. All network access goes through a trusted proxy sidecar that:
- Filters outbound traffic by domain allowlist
- Logs all requests
- Mediates package installations (verified mode)

Sudo is removed from the image, so the agent cannot escalate privileges, modify iptables, or bypass the proxy.

**Requires**: managed mode (compose or kubernetes), internal network.

**Default allowed domains** (configurable per-project):
- `github.com`, `*.githubusercontent.com`
- `pypi.org`, `*.pythonhosted.org`
- `registry.npmjs.org`

### `airgapped` — Total Isolation

```yaml
agent.sudo: false
network.outbound: none
install.mode: none
audit.mode: none
```

No network, no sudo, no installs. The agent works only with what's baked into the image. Use for maximum isolation when reviewing untrusted code.

**Requires**: any environment. Works with `--network=none` on plain Docker.

## Capability Matrix

Each security profile requires certain capabilities from the environment. Skua validates this at config time and blocks `skua run` if requirements aren't met.

```
Profile      Sudo  Internet  Installs    Audit     Min Mode     Min Env
─────────────────────────────────────────────────────────────────────────
open         yes   direct    unrestricted none      unmanaged    docker+bridge
standard     yes   direct    advisory     advisory  unmanaged    docker+bridge
hardened     no    proxy     verified     trusted   managed      compose+internal
airgapped    no    none      none         none      unmanaged    docker+none
```

## Container Runtime Isolation

Independent of security profiles, you can strengthen container isolation by choosing a different OCI runtime:

| Runtime | Isolation Level | Description |
|---------|----------------|-------------|
| (default/runc) | Container | Standard Linux namespaces and cgroups |
| `runsc` (gVisor) | Kernel sandbox | Intercepts all syscalls in userspace; agent never talks to host kernel |
| `kata` | MicroVM | Runs workload in a lightweight VM with its own kernel |

Configure via the Environment resource:

```yaml
spec:
  docker:
    containerRuntime: runsc
```

gVisor is the recommended choice — it provides meaningful kernel isolation with minimal overhead and works as a drop-in Docker runtime (`docker run --runtime=runsc`).

## Validation

Skua validates configuration consistency before allowing `skua run`:

1. **Environment internal**: managed mode requires compose/k8s driver
2. **Security internal**: e.g., `verified` installs require `sudo: false`
3. **Security vs Environment**: required capabilities must be provided
4. **Agent vs Security**: e.g., agent login needs network, but security blocks it

```bash
# Check a project's configuration
skua validate myapp

# See full resolved config
skua describe myapp
```

## Progression Path

A typical progression as trust requirements increase:

```
Development     →  open + local-docker
Team project    →  standard + local-docker
Code review     →  hardened + local-compose
Security audit  →  hardened + local-compose (+ gVisor)
Untrusted code  →  airgapped + local-docker
```
