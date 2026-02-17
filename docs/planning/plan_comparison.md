<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Competitive Landscape & Comparison

## Market Categories

The AI agent sandboxing space breaks into five categories:

| Category | Examples | Relationship to Skua |
|----------|----------|---------------------|
| Cloud sandbox platforms | E2B, Daytona, Modal, Runloop, Northflank | Different market — cloud SaaS for building products |
| Docker Desktop integration | Docker Sandboxes | Same problem, different tech (microVM) and audience (Docker Desktop users) |
| Claude-specific Docker wrappers | ClaudeBox, claude-container, Trail of Bits devcontainer | Direct competitors — same problem space |
| Kubernetes-native | K8s Agent Sandbox (SIG Apps) | Aligned philosophy, different deployment target |
| Agent API/control planes | Rivet Sandbox Agent, Coder AgentAPI | Complementary — they control agents, we isolate them |

---

## Direct Competitors (Claude/Agent Docker Wrappers)

### Docker Sandboxes (Docker, Inc.)
**https://docs.docker.com/ai/sandboxes**

Official Docker product. Runs Claude Code, Codex, Copilot, Gemini, Kiro inside Firecracker microVMs on the developer's machine. Each sandbox gets its own Docker daemon.

- **Stronger isolation**: Firecracker microVMs vs our plain containers
- **Multi-agent out of the box**: 5+ agents supported
- **Docker-in-Docker**: Agents can safely build/run containers
- **No configuration system**: No YAML manifests, no security profiles, no graduated controls
- **Commercial**: Requires Docker Desktop (macOS/Windows only)
- **No Linux server support**: Can't run on headless Linux

**Takeaway**: Their microVM isolation is genuinely stronger. We should consider gVisor or similar as an optional isolation backend. But their lack of declarative configuration means users can't express security policies or share configs across teams.

### ClaudeBox (RchGrav)
**https://github.com/RchGrav/claudebox**

Full-featured Docker environment for Claude Code with language stack profiles, MCP servers, and per-project isolation.

- **Language profiles**: Pre-configured stacks for C/C++, Python, Rust, Go, etc.
- **MCP server integration**: Built-in support for Model Context Protocol tools
- **Rich shell**: zsh + oh-my-zsh + powerline + fzf + syntax highlighting
- **Tmux integration**: Multi-pane workflows
- **Interactive CLI**: Profile-driven, not declarative YAML
- **No security tiers**: No graduated security model
- **No network filtering**: No proxy-based domain allowlisting

**Takeaway**: Their language profiles and MCP integration are features we should consider. The rich shell environment is nice but opinionated — we could offer it as an optional layer. Their interactive CLI approach is more approachable for beginners; our declarative YAML is better for reproducibility and team sharing.

### claude-container (nezhar)
**https://github.com/nezhar/claude-container**

Minimal Docker wrapper with an interesting twist: an API request logging proxy.

- **Request logging proxy**: Tracks all API requests in SQLite
- **Datasette visualization**: Browse/analyze logged API usage
- **Zero-config**: Single `docker run` command
- **No security controls**: No profiles, network filtering, or graduated isolation

**Takeaway**: The API request logging is directly relevant to our Phase 7 (Trusted API Proxy). Their approach of logging to SQLite + Datasette is a clean, lightweight pattern for the advisory tier of our audit system.

### Trail of Bits claude-code-devcontainer
**https://github.com/trailofbits/claude-code-devcontainer**

Security-focused devcontainer built for code auditing. From a respected security firm.

- **Security audit focus**: Designed for reviewing malicious code
- **VS Code devcontainer format**: IDE integration
- **Read-only git identity**: Mounted read-only, can't be modified by agent
- **No Docker socket**: Explicitly excluded
- **Optional iptables**: Network restriction as opt-in
- **No configuration management**: Single hardcoded setup

**Takeaway**: Their read-only git identity mounting is a good security practice we should adopt. The VS Code devcontainer format is worth supporting as an export target. Their security-audit-specific design validates our hardened/airgapped profiles.

### centminmod claude-code-devcontainers
**https://github.com/centminmod/claude-code-devcontainers**

Three-layer security architecture with iptables firewall, IPv6 protection, and dynamic IP whitelisting. Bundles Claude + Codex + Gemini.

- **Multi-agent**: Claude Code + Codex CLI + Gemini CLI in one container
- **iptables firewall**: Default-deny with whitelist (hardcoded domains)
- **IPv6 protection**: Blocks IPv6 to prevent firewall bypass
- **Dynamic IP whitelisting**: Updates firewall rules for changing IPs
- **Devcontainer format**: VS Code integration
- **Hardcoded rules**: No abstraction for switching security levels

**Takeaway**: Their IPv6 blocking is a real concern we haven't addressed — agents could bypass IPv4 firewall rules via IPv6. Their dynamic IP whitelisting for domain-based rules is practical. We should ensure our network controls cover both IPv4 and IPv6.

---

## Cloud Platforms (Different Market, Useful Ideas)

### E2B
**https://e2b.dev** | **https://github.com/e2b-dev/E2B**

Cloud infrastructure for running AI-generated code in Firecracker microVMs. Used by 88% of Fortune 100.

- Sub-200ms sandbox startup
- Python/JS SDKs
- 200+ MCP tool integrations
- Custom sandbox templates from Dockerfiles

**Relevant ideas**: Their custom sandbox templates from Dockerfiles is similar to our image building. Their MCP catalog integration is a feature gap for us.

### Daytona
**https://daytona.io** | **https://github.com/daytonaio/daytona**

Pivoted from dev environments to AI agent infrastructure. Declarative image builder where agents specify requirements and the system builds on-the-fly.

- Sub-90ms container startup
- Declarative image builder
- Python/TypeScript SDKs

**Relevant ideas**: Their on-the-fly environment building from agent declarations is interesting — instead of pre-building images, build them dynamically based on what the agent needs.

### Kubernetes Agent Sandbox (SIG Apps)
**https://github.com/kubernetes-sigs/agent-sandbox**

Official Kubernetes subproject. CRD for managing isolated AI agent workloads with gVisor/Kata backends. Backed by Google.

- Kubernetes-native CRD (`Sandbox` resource)
- gVisor and Kata Containers isolation
- Sub-second startup
- Stateful, long-running, singleton model

**Relevant ideas**: Their CRD design validates our Kubernetes-style YAML approach. When we add Kubernetes as a deployment target, we should consider compatibility with their `Sandbox` CRD format. Their gVisor/Kata support shows the direction for stronger isolation.

---

## Agent Control Planes (Complementary)

### Rivet Sandbox Agent
**https://github.com/rivet-dev/sandbox-agent**

Universal HTTP API (single Rust binary) for controlling Claude Code, Codex, OpenCode, and Amp. Deploys to E2B, Daytona, or Vercel Sandboxes.

- One HTTP API for multiple agents
- Universal event schema for logging/replay/audit
- Session persistence to Postgres/ClickHouse

**Relevant ideas**: Their universal event schema for audit logging is exactly what our Phase 7 proxy needs. Their agent-swapping capability shows how multi-agent support should work at the API level.

### Coder AgentAPI
**https://github.com/coder/agentapi**

Go HTTP server wrapping 11+ coding agents via terminal emulation. REST API: POST /message, GET /status, GET /events (SSE).

- Supports Claude, Goose, Aider, Gemini, Copilot, Amp, Codex, Auggie, Cursor CLI, and more
- Terminal emulation approach (wraps any CLI agent)
- OpenAPI schema

**Relevant ideas**: Their terminal emulation approach for wrapping arbitrary agents is clever — we could use a similar technique to support agents that don't have clean CLI interfaces. Their 11+ agent support shows the breadth of the market.

---

## What They Do That We Don't

### High Priority (should incorporate)

1. **Multi-agent support**: ClaudeBox, centminmod, Rivet, and Coder AgentAPI all support multiple agents. We have the AgentConfig abstraction but only ship a Claude preset. Adding Codex, Aider, and Gemini presets would be straightforward.

2. **MCP server integration**: ClaudeBox, E2B, and AIO Sandbox all integrate with Model Context Protocol. MCP tools extend agent capabilities significantly.

3. **API request logging/observability**: claude-container logs all API requests to SQLite. This is a lightweight version of our Phase 7 proxy that could be shipped much sooner as an advisory-tier feature.

4. **IPv6 firewall bypass protection**: centminmod explicitly blocks IPv6 to prevent agents from bypassing IPv4 firewall rules. Our network controls need to address this.

5. **Read-only mounts for sensitive config**: Trail of Bits mounts `.gitconfig` read-only. We should do the same for git identity and SSH keys — prevent the agent from modifying credentials.

### Medium Priority (consider for roadmap)

6. **VS Code devcontainer export**: Trail of Bits and centminmod use devcontainer format. We could add `skua export --format devcontainer` to generate `.devcontainer/devcontainer.json` from our YAML config.

7. **HTTP API for programmatic control**: Rivet and Coder AgentAPI show demand for controlling agents via HTTP. This would enable web UIs, CI/CD integration, and agent-to-agent orchestration.

8. **Stronger isolation backends**: Docker Sandboxes uses Firecracker, K8s Agent Sandbox uses gVisor/Kata. We could optionally support gVisor (`--runtime=runsc`) for users who want stronger isolation without leaving Docker.

9. **Language/stack profiles**: ClaudeBox's pre-configured language stacks reduce setup friction. We could ship these as additional Environment presets (e.g., `python-dev.yaml`, `rust-dev.yaml`).

10. **Dynamic IP whitelisting**: centminmod resolves domain names to IPs and updates firewall rules. Useful for our proxy-based filtering in hardened mode.

### Low Priority (nice to have)

11. **Browser/GUI access**: AIO Sandbox bundles VNC browser and VS Code Server. Useful for agents that need web interaction but adds significant complexity.

12. **On-the-fly image building**: Daytona builds images dynamically from agent declarations. Interesting but our pre-built image approach is simpler and more predictable.

13. **Cloud deployment**: E2B, Daytona, Modal, Runloop all offer cloud-hosted sandboxes. We're local-first by design, but a future `skua deploy` to a cloud provider could be valuable.

---

## What We Do That They Don't

### Unique to Skua

1. **Kubernetes-style declarative YAML with distinct resource kinds**: No other local CLI tool separates Environment, SecurityProfile, AgentConfig, and Project into independent, composable resources. This is our strongest architectural differentiator.

2. **Graduated security profiles with capability validation**: The open/standard/hardened/airgapped progression with compile-time validation that security requirements match deployment capabilities is unique. Everyone else is either "open" or "locked down" with no middle ground.

3. **Proxy-based domain allowlisting in hardened mode**: Most tools either allow all network or block all network. Our planned proxy-based filtering allows specific domains (npm, PyPI, GitHub) while blocking everything else.

4. **Security-deployment consistency enforcement**: No other tool validates that your security policy is actually enforceable by your deployment environment before you run.

5. **Agent-agnostic with per-agent YAML configs**: While others hardcode agent support or bundle multiple agents, our AgentConfig resource cleanly abstracts agent-specific concerns (install commands, auth, runtime flags).

6. **Airgapped mode**: True network-none isolation. Most tools assume internet access.

7. **SSH key management**: Integrated SSH key discovery, per-project key assignment, and secure key injection into containers. Most competitors either ignore this or leave it to the user.

---

## How Our Approach Differs

| Dimension | Skua | Most Competitors |
|-----------|------|-----------------|
| **Config model** | Declarative YAML resources (K8s-style) | Imperative CLI flags or hardcoded |
| **Security** | Graduated profiles with validation | Binary (open or locked) |
| **Deployment** | Local-first, self-hosted | Cloud SaaS or Docker Desktop |
| **Agent support** | Agent-agnostic with YAML abstraction | Hardcoded for specific agents |
| **Isolation** | Docker containers (planned: gVisor) | Varies: containers, microVMs, none |
| **Interface** | CLI + YAML files | SDK/API, GUI, or devcontainer |
| **Trust model** | Explicit (advisory vs verified tiers) | Implicit or absent |
| **Composability** | Mix-and-match resources by reference | Monolithic configuration |

### Our core thesis
Security for AI coding agents isn't binary. Different projects need different security levels, and the tooling should make it easy to express, validate, and enforce graduated security policies declaratively. Configuration should be composable, shareable, and version-controllable — like Kubernetes manifests, not like CLI flags.

### Where we're weaker
- **Isolation strength**: Plain Docker containers are the weakest isolation boundary in this space. Firecracker (Docker Sandboxes, E2B) and gVisor/Kata (K8s Agent Sandbox) are meaningfully stronger.
- **Multi-agent breadth**: We only ship a Claude preset. The market is moving to multi-agent fast.
- **Observability**: We have no request logging, event streaming, or audit trail yet.
- **Ecosystem integration**: No MCP support, no VS Code integration, no HTTP API.

### Where we're stronger
- **Configuration model**: Nobody else has composable, typed YAML resources with cross-resource validation.
- **Security granularity**: Nobody else offers graduated security with capability matrices.
- **Simplicity**: We're a CLI tool that works on any Linux box with Docker. No cloud account, no Docker Desktop, no Kubernetes cluster needed.
- **Transparency**: Users can read and edit YAML files. No opaque configuration databases or GUI-only settings.

---

## Recommended Incorporations (Priority Order)

1. **Multi-agent presets** — Ship AgentConfig YAMLs for Codex, Aider, Gemini CLI. Low effort, high value.
2. **Read-only credential mounts** — Mount git identity and SSH keys as read-only in containers. Security hardening.
3. **IPv6 blocking** — Add `--sysctl net.ipv6.conf.all.disable_ipv6=1` to container runs when network filtering is active.
4. **API request logging** — Advisory-tier SQLite logging proxy (lighter than Phase 7 full proxy). Ship as `standard` profile feature.
5. **gVisor support** — Optional `--runtime=runsc` flag in Environment config for stronger isolation.
6. **MCP server support** — Allow AgentConfig to declare MCP servers to mount/configure in the container.
7. **VS Code devcontainer export** — `skua export --format devcontainer <project>` to generate `.devcontainer/` from YAML.
8. **HTTP API** — Optional `skua serve` mode for programmatic control. Enables web UIs and CI/CD integration.
