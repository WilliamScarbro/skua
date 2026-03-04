<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Agent Activity Monitoring

`skua list` includes an **ACTIVITY** column that shows whether a
running agent is actively processing, has recent API activity, is idle
between turns, or has finished its task.

```
NAME             SOURCE                                 AGENT      CREDENTIAL           ACTIVITY       STATUS
-----------------------------------------------------------------------------------------------------------------
myapp            DIR:~/projects/myapp                   claude     (none)               think:Bash     running
other            DIR:~/projects/other                   claude     (none)               idle           running
codex-proj       DIR:~/projects/codex-proj              codex      (none)               done           running
codex-busy       DIR:~/projects/codex-busy              codex      (none)               XXXX           running
stale            DIR:~/projects/stale                   claude     (none)               -              running
```

Possible ACTIVITY values:

| Value            | Meaning                                             |
|------------------|-----------------------------------------------------|
| `thinking`       | Agent is executing a tool (no tool name available)  |
| `think:<Tool>`   | Agent is executing the named tool                   |
| `processing`     | Codex has an active subprocess                      |
| `X` to `XXXXXX`  | Codex API activity over the last 30 seconds         |
| `idle`           | Agent is between tool calls, waiting for the model  |
| `done`           | Agent finished its task (Stop event fired)          |
| `-`              | Status unavailable — container pre-dates monitoring |
|                  | support, no agent has been started yet, or hook     |
|                  | setup failed silently                               |
| `?`              | Status file present but unreadable                  |

## How It Works (Unmanaged Mode)

Skua injects lightweight hook scripts into the container image at build time.
On each container start, `entrypoint.sh` runs the agent-specific setup script
from `/home/dev/.entrypoint.d/` which configures the hooks and initialises
`/tmp/skua-agent-status`.

### Claude Code

Claude Code has a formal hooks API.  The setup script (`.entrypoint.d/claude.sh`)
merges four hook entries into `~/.claude/settings.local.json` at startup — the
merge is idempotent, so restarting the container does not duplicate entries:

| Hook event    | Writes to status file              |
|---------------|------------------------------------|
| `PreToolUse`  | `{"state":"thinking","tool":"...","ts":...}` |
| `PostToolUse` | `{"state":"idle","ts":...}`        |
| `Stop`        | `{"state":"done","ts":...}`        |
| `SubagentStop`| `{"state":"done","ts":...}`        |

The hook scripts live at `/home/dev/.entrypoint.d/hooks/` inside the container.

Because `settings.local.json` is in the persistent auth volume, the hooks
survive container restarts without re-merging.

> **Unmanaged advisory note**: In unmanaged mode the agent has full write
> access inside its container, including to `/tmp/skua-agent-status` and
> `settings.local.json`.  A sufficiently determined agent could tamper with
> the status file.  The ACTIVITY column should be treated as informational,
> not authoritative.

### Codex

Codex does not expose a formal hooks API.  Instead `.entrypoint.d/codex.sh`
starts a background bash daemon (`hooks/codex-monitor.sh`) that polls the
Codex runtime once per second and watches API traffic:

- **Descendant subprocesses present** → agent is running a tool → writes `processing`
- **At least 100 API hits in the last 30 seconds** → shows API activity in the ACTIVITY column
- **Fewer than 100 API hits in the last 30 seconds** → shows `idle`
- **Process exits** → writes `done`

The display is calibrated for Codex's current traffic profile:

| Hits in last 30s | Display |
|------------------|---------|
| `< 100`          | `idle`  |
| `100-249`        | `X`     |
| `250-399`        | `XX`    |
| `400-549`        | `XXX`   |
| `550-699`        | `XXXX`  |
| `700-849`        | `XXXXX` |
| `>= 850`         | `XXXXXX` |

The daemon is lightweight (a `while sleep 1` loop) and exits with the
container.

---

## Future: Managed Mode Monitoring

In **managed mode** skua runs as a sidecar container alongside the agent
(requires `driver: compose` or `driver: kubernetes`).  The sidecar sits
outside the agent's trust boundary, making monitoring tamper-resistant.

### Recommended Architecture

```
┌─────────────────────────────────┐   ┌──────────────────────┐
│  Agent container                │   │  Skua sidecar        │
│                                 │   │                       │
│  claude / codex                 │   │  • MCP server        │
│      │ LLM API calls            │   │  • Status aggregator │
│      │                          │   │  • Proxy (optional)  │
│      └──► MCP: localhost:PORT ──┼───►  Trusted endpoint    │
│                                 │   │                       │
│  /tmp/skua-agent-status  ───────┼───► read via shared vol  │
└─────────────────────────────────┘   └──────────────────────┘
```

### Implementation Options

#### Option A — Shared Volume Status File (simplest)

Mount a small shared volume between the agent and sidecar containers.  The
agent writes its status there (via the same hook scripts used in unmanaged
mode) and the sidecar reads it authoritatively.  The sidecar exposes the
aggregated status via a Unix socket or small HTTP endpoint that `skua list`
queries instead of using `docker exec`.

```yaml
# docker-compose.yml skeleton
services:
  agent:
    volumes:
      - skua-status:/run/skua          # shared status volume
  sidecar:
    volumes:
      - skua-status:/run/skua:ro       # sidecar reads only
    ports:
      - "127.0.0.1:9273:9273"          # status API

volumes:
  skua-status:
```

The sidecar exposes a minimal HTTP endpoint:

```
GET /status  →  {"state":"thinking","tool":"Bash","ts":1712345678}
```

`skua list` would query this endpoint instead of running `docker exec`.  The
sidecar validates the JSON schema and can detect stale timestamps (e.g. hook
script crashed), returning `{"state":"stale"}` if the file has not been
updated within a configurable TTL.

#### Option B — Proxy-Mediated Detection (most accurate)

The sidecar hosts the outbound proxy for LLM API traffic (required for the
`proxy` security profile).  It can infer agent state from the HTTP stream:

- **Active streaming response** → `thinking`
- **Between requests / idle connection** → `idle`
- **Session ended** → `done`

This requires no cooperation from the agent at all — it works even if the
hook scripts are removed or tampered with.  The status is derived from
observable network facts rather than from in-container writes.

#### Option C — Trusted MCP Tool

The sidecar exposes an MCP tool (e.g. `report_status`) that the agent calls
explicitly at the start and end of tasks.  The sidecar rejects status updates
that do not come through the MCP channel, making spoofing impossible.

This approach is the most structured but requires the agent to cooperate by
calling the tool — best implemented by including an instruction in
`AGENTS.md` / `CLAUDE.md` and potentially enforcing it via a `PreToolUse`
hook that checks whether the agent has called `report_status` recently.

### Consuming Managed-Mode Status in `skua list`

When the project's environment is `mode: managed`, `skua list` should:

1. Resolve the sidecar endpoint address from the environment config.
2. Issue a short-timeout HTTP GET to the status endpoint.
3. Fall back to `docker exec cat /tmp/skua-agent-status` if the endpoint is
   unreachable (degraded-mode compatibility with unmanaged images).

The `_agent_activity()` function in `list_cmd.py` is the right extension
point — it currently dispatches via `docker exec`; a managed-mode branch
would dispatch to the HTTP endpoint instead.
