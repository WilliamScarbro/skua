<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Skua Configuration Model

## Design Principles

1. **Kubernetes-style declarative resources** — YAML files with `apiVersion`, `kind`, `metadata`, `spec`
2. **Separation of concerns** — deployment, security, and agent configs are independent resources that reference each other
3. **Consistency enforcement** — skua validates that a security profile's requirements are satisfiable by the referenced deployment context before allowing a project to run
4. **Progressive disclosure** — simple projects need one file; advanced setups compose multiple resources

## Resource Types

```
Environment    — where and how containers run
SecurityProfile — what the agent is allowed to do
AgentConfig     — which agent and how it's installed
Project         — ties the above together for a specific codebase
```

---

## 1. Environment

Describes the deployment target and its capabilities. Each environment has a **mode** that determines the trust boundary:

- **unmanaged** — single agent container, skua launches and exits (`execvp`). Advisory-only monitoring. Internal MCP servers (agent can tamper). Simple, lightweight.
- **managed** — skua runs as a sidecar container alongside the agent. Trusted MCP endpoints, verified monitoring, proxy-mediated network. Requires `compose` or `kubernetes` driver.

```yaml
apiVersion: skua/v1
kind: Environment
metadata:
  name: local-docker
spec:
  mode: unmanaged            # unmanaged | managed
  driver: docker             # docker | compose | kubernetes

  # driver: docker
  docker:
    runtime: local           # local | remote
    # remote:
    #   host: ssh://user@host
    cleanup: ephemeral       # ephemeral (--rm) | persistent (keep container)
    containerRuntime: ""     # "" (default/runc) | runsc (gVisor) | kata

  # driver: compose
  # compose:
  #   runtime: local
  #   cleanup: ephemeral

  # driver: kubernetes
  # kubernetes:
  #   context: my-cluster
  #   namespace: skua
  #   storageClass: standard

  persistence:
    mode: bind               # bind | volume
    # bind-specific
    basePath: ~/.config/skua/claude-data
    # volume-specific
    # volumePrefix: skua

  network:
    mode: bridge             # none | bridge | internal | host
    # bridge: default docker bridge, container has internet
    # internal: no outbound internet (requires compose or k8s for sidecar)
    # none: no network at all
    # host: host network (no isolation)
```

### Mode: Unmanaged vs Managed

The mode is the primary architectural decision for an environment:

```
                 Unmanaged                     Managed
─────────────────────────────────────────────────────────────────────
Containers       1 (agent only)                2+ (agent + skua sidecar)
MCP servers      Internal (agent can tamper)   External (trusted boundary)
Monitoring       Advisory (in-container)       Verified (sidecar-mediated)
Network proxy    Not available                 Trusted proxy sidecar
Audit            Advisory (EXIT traps)         Trusted (proxy logs)
Driver           docker, compose, or k8s       compose or k8s only
Complexity       Minimal                       Higher (multi-container)
```

### Container Runtime

The `containerRuntime` field controls the OCI runtime used for Docker containers:

- `""` (empty/default) — uses Docker's default runtime (typically `runc`)
- `runsc` — gVisor sandbox, intercepts all syscalls in userspace (requires gVisor installed)
- `kata` — Kata Containers, runs workloads in lightweight VMs (requires Kata installed)

gVisor provides stronger isolation than plain containers without requiring full VMs. The agent process never talks directly to the host kernel.

### Capability Matrix

Each environment mode + network combination **provides** a set of capabilities. Security profiles **require** capabilities. Skua validates that all required capabilities are provided.

```
                          unmanaged  unmanaged  managed    managed
Capability                bridge     none       bridge     internal
─────────────────────────────────────────────────────────────────────
network.internet          yes        no         yes        no
network.isolation         yes        yes        yes        yes
network.internal          no         yes        no         yes
sidecar                   no         no         yes        yes
trusted.proxy             no         no         yes        yes
trusted.log               no         no         yes        yes
trusted.mcp               no         no         yes        yes
container.sudo            yes        yes        yes        yes
container.no-sudo         yes        yes        yes        yes
isolation.gvisor          *          *          *          *
audit.docker-diff         **         **         **         **
```

`*` Only when `containerRuntime: runsc`
`**` requires `cleanup: persistent` (non-`--rm`)

**Key constraint:** `managed` mode requires `compose` or `kubernetes` driver. The skua sidecar needs multi-container orchestration.

---

## 2. SecurityProfile

Declares what the agent is and isn't allowed to do. Each rule implicitly **requires** certain environment capabilities.

```yaml
apiVersion: skua/v1
kind: SecurityProfile
metadata:
  name: standard
spec:
  # Network access policy
  network:
    outbound: unrestricted   # unrestricted | none | proxy
    # unrestricted: agent has direct internet (requires: network.internet)
    # none: no network at all (requires: network.isolation)
    # proxy: agent internet goes through trusted proxy (requires: trusted.proxy)

    # Only meaningful when outbound: proxy
    proxy:
      allowedDomains:
        - github.com
        - "*.githubusercontent.com"
        - pypi.org
        - "*.pythonhosted.org"
        - registry.npmjs.org
      logRequests: true

  # Agent privilege level inside container
  agent:
    sudo: false              # true | false
    # false requires: container.no-sudo (always available)
    # true requires: container.sudo (always available)

  # Software install policy
  install:
    mode: none               # unrestricted | advisory | verified | none
    # unrestricted: agent can apt/pip freely (requires: agent.sudo: true)
    # advisory: in-container wrappers log installs, unverified (requires: agent.sudo: true)
    # verified: installs go through trusted proxy (requires: trusted.proxy, agent.sudo: false)
    # none: no installs allowed (requires: agent.sudo: false)

    # Only meaningful when mode: verified
    verified:
      autoApprove: []        # packages approved without user confirmation
      # autoApprove: [nodejs, npm, python3-dev]

  # Audit & tracking
  audit:
    mode: none               # none | advisory | trusted
    # none: no tracking
    # advisory: in-container EXIT trap logs to volume (unverified)
    # trusted: proxy logs all mediated actions (requires: trusted.log)

  # Image adaptability
  imageUpdates:
    mode: disabled           # disabled | suggest | auto
    # disabled: no image update suggestions
    # suggest: present install log on next run, user approves
    # auto: auto-rebuild project image with approved packages
    source: audit            # audit | proxy
    # audit: reads from audit log (advisory or trusted depending on audit.mode)
    # proxy: reads from proxy log only (requires: trusted.log)
```

### Implicit Capability Requirements

Each setting implies requirements on the environment. Skua derives these automatically:

```
Setting                          Requires capability
──────────────────────────────────────────────────────────────
network.outbound: unrestricted → network.internet
network.outbound: none         → network.isolation
network.outbound: proxy        → trusted.proxy, network.internal
install.mode: unrestricted     → (agent.sudo must be true)
install.mode: advisory         → (agent.sudo must be true)
install.mode: verified         → trusted.proxy, (agent.sudo must be false)
install.mode: none             → (agent.sudo must be false)
audit.mode: trusted            → trusted.log
imageUpdates.source: proxy     → trusted.log
```

### Internal Consistency Rules

Within a single SecurityProfile, skua validates:

```
Rule                                              Rationale
──────────────────────────────────────────────────────────────────────────────────────
install.mode: verified  → agent.sudo: false       Can't verify if agent bypasses proxy
install.mode: none      → agent.sudo: false       No point blocking installs if agent has sudo
install.mode: advisory  → agent.sudo: true        Wrappers need packages to actually install
install.mode: unrestricted → agent.sudo: true     Agent needs sudo to install
network.outbound: proxy → agent.sudo: false*      Agent could bypass proxy via raw sockets
audit.mode: trusted     → network.outbound: proxy Trusted audit requires proxy mediation
imageUpdates.source: proxy → audit.mode: trusted  Can't read proxy log without trusted audit
```

`*` Strictly, sudo lets the agent modify iptables/routing to bypass the proxy. Without sudo, the internal network + proxy is enforced by Docker at a level the agent can't reach.

---

## 3. AgentConfig

Describes an AI agent: how to install it, how to authenticate, how to run it.

```yaml
apiVersion: skua/v1
kind: AgentConfig
metadata:
  name: claude
spec:
  install:
    commands:
      - "curl -fsSL https://claude.ai/install.sh | bash"
    # Packages that must be in the base image (not agent-installed)
    requiredPackages: []

  runtime:
    command: claude
    env:
      PATH: "/home/dev/.local/bin:${PATH}"
      EDITOR: vim
    entrypointHooks:
      - entrypoint.d/claude.sh

  auth:
    dir: .claude              # mounted as volume/bind for persistence
    files:                    # files that constitute credentials
      - .credentials.json
      - .claude.json
    loginCommand: "claude login"

  permissions:
    # Agent-specific permission presets (e.g., Claude's settings.json)
    presets:
      strict:
        settings:
          permissions:
            allow: []
      standard:
        settings:
          permissions:
            allow:
              - "Bash(git:*)"
              - "Bash(python3:*)"
      permissive:
        settings:
          permissions:
            allow:
              - "Bash(*)"
```

---

## 4. Project

Ties everything together. References an Environment, SecurityProfile, and AgentConfig by name.

```yaml
apiVersion: skua/v1
kind: Project
metadata:
  name: my-app
spec:
  directory: /home/user/projects/my-app

  environment: local-docker     # references Environment by name
  security: standard            # references SecurityProfile by name
  agent: claude                 # references AgentConfig by name

  # Per-project overrides (merged on top of referenced resources)
  overrides:
    # Override specific security settings for this project
    security:
      network:
        outbound: proxy
        proxy:
          allowedDomains:
            - github.com
            - "*.crates.io"

    # Override agent settings for this project
    agent:
      permissions:
        preset: strict

  # Project-specific image extensions
  image:
    extraPackages: [nodejs, npm]
    extraCommands:
      - "npm install -g typescript"

  # Git identity (inherits from global if not set)
  git:
    name: ""                    # falls back to global
    email: ""

  # SSH key (inherits from global if not set)
  ssh:
    privateKey: ~/.ssh/id_ed25519
```

---

## 5. Shipped Presets

Skua ships with built-in profiles users can reference or extend. These live in the skua install directory and are copied to `~/.config/skua/` on init.

### Environments

```yaml
# environments/local-docker.yaml — simplest setup, unmanaged
apiVersion: skua/v1
kind: Environment
metadata:
  name: local-docker
spec:
  mode: unmanaged
  driver: docker
  docker:
    runtime: local
    cleanup: ephemeral
  persistence:
    mode: bind
    basePath: ~/.config/skua/claude-data
  network:
    mode: bridge
---
# environments/local-docker-gvisor.yaml — unmanaged with gVisor isolation
apiVersion: skua/v1
kind: Environment
metadata:
  name: local-docker-gvisor
spec:
  mode: unmanaged
  driver: docker
  docker:
    runtime: local
    cleanup: ephemeral
    containerRuntime: runsc
  persistence:
    mode: bind
    basePath: ~/.config/skua/claude-data
  network:
    mode: bridge
---
# environments/local-compose.yaml — managed with skua sidecar
apiVersion: skua/v1
kind: Environment
metadata:
  name: local-compose
spec:
  mode: managed
  driver: compose
  compose:
    runtime: local
    cleanup: ephemeral
  persistence:
    mode: volume
    volumePrefix: skua
  network:
    mode: internal
```

### Security Profiles

```yaml
# security/open.yaml — no restrictions, maximum agent capability
apiVersion: skua/v1
kind: SecurityProfile
metadata:
  name: open
spec:
  network:
    outbound: unrestricted
  agent:
    sudo: true
  install:
    mode: unrestricted
  audit:
    mode: none
  imageUpdates:
    mode: disabled
---
# security/standard.yaml — advisory tracking, agent has sudo
apiVersion: skua/v1
kind: SecurityProfile
metadata:
  name: standard
spec:
  network:
    outbound: unrestricted
  agent:
    sudo: true
  install:
    mode: advisory
  audit:
    mode: advisory
  imageUpdates:
    mode: suggest
    source: audit
---
# security/hardened.yaml — no sudo, no direct internet, proxy-mediated
apiVersion: skua/v1
kind: SecurityProfile
metadata:
  name: hardened
spec:
  network:
    outbound: proxy
    proxy:
      allowedDomains:
        - github.com
        - "*.githubusercontent.com"
        - pypi.org
        - "*.pythonhosted.org"
      logRequests: true
  agent:
    sudo: false
  install:
    mode: verified
    verified:
      autoApprove: []
  audit:
    mode: trusted
  imageUpdates:
    mode: suggest
    source: proxy
---
# security/airgapped.yaml — total isolation
apiVersion: skua/v1
kind: SecurityProfile
metadata:
  name: airgapped
spec:
  network:
    outbound: none
  agent:
    sudo: false
  install:
    mode: none
  audit:
    mode: none
  imageUpdates:
    mode: disabled
```

---

## 6. Validation Rules

When a Project is created or modified, skua validates:

### Step 1: SecurityProfile internal consistency

Check the rules table in section 2. Example violations:
```
ERROR: install.mode 'verified' requires agent.sudo to be false, but agent.sudo is true
ERROR: audit.mode 'trusted' requires network.outbound to be 'proxy', but it is 'unrestricted'
```

### Step 2: SecurityProfile → Environment capability check

Derive required capabilities from the SecurityProfile, check the Environment provides them:
```
ERROR: security 'hardened' requires capability 'trusted.proxy',
       but environment 'local-docker' (driver: docker) does not provide it.
       Hint: use environment 'local-compose' or 'kubernetes' for proxy support.

ERROR: security 'standard' requires capability 'network.internet',
       but environment 'local-compose' has network.mode 'internal'.
       Hint: change network.mode to 'bridge', or change security to not require internet.
```

### Step 3: AgentConfig compatibility

Check that the agent's requirements are compatible with the security profile:
```
WARNING: agent 'claude' loginCommand requires network access,
         but security 'airgapped' has network.outbound 'none'.
         You'll need to pre-authenticate before running in airgapped mode.
```

### Validation output format

```
$ skua validate my-app

Project: my-app
  Environment:  local-compose     ✓
  Security:     hardened          ✓
  Agent:        claude            ✓

  Consistency checks:
    ✓ install.mode 'verified' consistent with agent.sudo: false
    ✓ network.outbound 'proxy' consistent with audit.mode: trusted
    ✓ environment 'local-compose' provides: trusted.proxy, trusted.log, network.internal

  Warnings:
    ⚠ proxy.allowedDomains does not include claude.ai — agent login may fail

  Result: VALID
```

---

## 7. File Layout

```
~/.config/skua/
├── config.yaml                  # global defaults (git identity, default environment/security/agent)
├── environments/
│   ├── local-docker.yaml        # shipped preset
│   ├── local-compose.yaml       # shipped preset
│   └── my-cluster.yaml          # user-defined
├── security/
│   ├── open.yaml                # shipped preset
│   ├── standard.yaml            # shipped preset
│   ├── hardened.yaml            # shipped preset
│   ├── airgapped.yaml           # shipped preset
│   └── my-custom.yaml           # user-defined
├── agents/
│   ├── claude.yaml              # shipped preset
│   ├── aider.yaml               # shipped preset
│   └── my-agent.yaml            # user-defined
├── projects/
│   ├── my-app.yaml
│   └── other-project.yaml
└── claude-data/                 # persistence (bind mode)
    ├── my-app/
    └── other-project/
```

---

## 8. CLI Integration

```bash
# Quick start — uses global defaults for everything
skua init                        # first-time wizard, sets default env/security/agent
skua add my-app --dir ./my-app   # creates project with defaults

# Explicit resource selection
skua add my-app --dir ./my-app --env local-compose --security hardened --agent claude

# Validate before running
skua validate my-app

# Run (validates implicitly)
skua run my-app

# List available resources
skua env list
skua security list
skua agents list

# Inspect what a project resolves to
skua describe my-app

# Edit resources
skua env edit local-docker
skua security edit standard
```

---

## 9. Dependency Summary

```
             requires          requires              requires
  airgapped ────────→ none    │  hardened ──────────→ managed mode
  (any mode)         network  │  (needs proxy/MCP)   (compose/k8s)
                              │
  open ─────────────→ any     │  standard ──────────→ any
  (no restrictions)  mode     │  (advisory only)      mode with internet
```

The four shipped security profiles map to a clean progression:

| Profile | Sudo | Internet | Install | Audit | Min. Mode | Min. Environment |
|---|---|---|---|---|---|---|
| open | yes | direct | unrestricted | none | unmanaged | docker + bridge |
| standard | yes | direct | advisory | advisory | unmanaged | docker + bridge |
| hardened | no | proxy | verified | trusted | managed | compose + internal |
| airgapped | no | none | none | none | unmanaged | docker + none* |

`*` Or any environment — airgapped doesn't need network capabilities.

**gVisor** can be added to any docker/compose environment for stronger isolation, independent of mode or security profile. It provides kernel-level sandboxing that complements (but doesn't replace) the security profile controls.
