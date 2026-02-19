# SPDX-License-Identifier: BUSL-1.1
"""skua adapt — apply project image requests from template/flags."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from skua.commands.credential import resolve_credential_sources
from skua.config import ConfigStore, validate_project
from skua.docker import (
    build_run_command,
    build_image,
    image_exists,
    image_name_for_project,
    resolve_project_image_inputs,
)
from skua.project_adapt import (
    ensure_adapt_workspace,
    image_request_path,
    load_image_request,
    request_changes_project,
    request_has_updates,
    apply_image_request_to_project,
    write_applied_image_request,
)


def cmd_adapt(args):
    store = ConfigStore()
    all_mode = bool(getattr(args, "all", False))
    name = str(getattr(args, "name", "") or "").strip()

    if all_mode:
        _cmd_adapt_all(store, args)
        return

    if not name:
        print("Error: Provide a project name or use --all.")
        sys.exit(1)

    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)

    env = store.load_environment(project.environment)
    if env is None:
        print(f"Error: Environment '{project.environment}' not found.")
        sys.exit(1)
    if env.mode != "unmanaged":
        print(
            f"Error: skua adapt currently supports unmanaged mode only "
            f"(project uses mode '{env.mode}')."
        )
        sys.exit(1)
    sec = store.load_security(project.security)
    if sec is None:
        print(f"Error: Security profile '{project.security}' not found.")
        sys.exit(1)

    agent = store.load_agent(project.agent)
    if agent is None:
        print(f"Error: Agent '{project.agent}' not found.")
        sys.exit(1)

    if bool(getattr(args, "show_prompt", False)):
        prompt = _agent_prompt(project.name, (agent.name or "").strip().lower())
        command = _agent_adapt_command(agent, project.name)
        print(f"Adapt prompt for project '{project.name}' (agent: {agent.name}):")
        print()
        print(prompt)
        print()
        print("Resolved non-interactive agent command:")
        print(f"  {_shell_join(command)}")
        return

    if bool(getattr(args, "discover", False)) and bool(getattr(args, "apply_only", False)):
        print("Error: --discover and --apply-only cannot be used together.")
        sys.exit(1)

    project_dir = _ensure_project_directory(store, project)
    if project_dir is None:
        print("Error: project directory is not set.")
        sys.exit(1)

    guide_path, request_path = ensure_adapt_workspace(project_dir, project.name, project.agent)
    print(f"Adapt guide:   {guide_path}")
    print(f"Request file:  {request_path}")

    result = validate_project(project, env, sec, agent)
    if result.warnings:
        for warning in result.warnings:
            print(f"  Warning: {warning}")
    if not result.valid:
        print("\nConfiguration validation failed:")
        for error in result.errors:
            print(f"  x {error}")
        print("\nRun 'skua validate' for details, or fix the configuration.")
        sys.exit(1)

    request_from_flags = _request_from_flags(args)
    discover_mode = bool(getattr(args, "discover", False))
    if discover_mode and request_from_flags is not None:
        print("Warning: --discover ignored because request flags were provided.")
        discover_mode = False
    if discover_mode and args.clear:
        print("Warning: --discover ignored with --clear.")
        discover_mode = False

    if discover_mode:
        print("[adapt] Step 1: Discover wishlist with agent")
        _run_agent_adapt_session(store, project, env, sec, agent)

    if request_from_flags is not None:
        request = request_from_flags
        request_source = "flags"
    else:
        request = load_image_request(request_path)
        request_source = str(request_path)

    if args.write_only:
        print("Adapt files ensured. No image config was applied.")
        return

    if args.clear:
        request = {
            "schemaVersion": 1,
            "status": "ready",
            "summary": "Reset project image customization.",
            "baseImage": "",
            "fromImage": "",
            "packages": [],
            "commands": [],
        }

    if not args.clear and not request_has_updates(request):
        print("No requested image changes found.")
        if discover_mode:
            print("Agent did not request any image customizations.")
        else:
            print("No latent image-request updates to apply.")
            print(f"Run 'skua adapt {project.name} --discover' to generate a new wishlist.")
        return

    if _is_interactive_tty():
        if not _confirm_apply_wishlist(project.agent, request):
            print("Adapt cancelled before applying wishlist.")
            return
    else:
        print("[adapt] Non-interactive mode: auto-approving wishlist.")

    changed = apply_image_request_to_project(project, request)
    print("[adapt] Step 2: Apply image request")
    if not changed:
        print("Project image configuration already matches request; no changes applied.")
    else:
        store.save_resource(project)
        print(f"Applied image request from: {request_source}")
        print(f"Project image version: v{project.image.version}")
        _print_project_image_summary(project)
        if request_source != "flags":
            write_applied_image_request(request_path, request, project.image.version)

    should_build = bool(getattr(args, "build", False) or discover_mode)
    if should_build and changed:
        print("[adapt] Step 3: Build adapted image")
        _build_project_image(store, project, agent)
    else:
        print(f"Next: run 'skua run {project.name}' to build/use the updated image.")


def _project_has_pending_request(project) -> bool:
    """Return True when project has unapplied image-request changes."""
    directory = str(getattr(project, "directory", "") or "").strip()
    if not directory:
        return False
    project_dir = Path(directory).expanduser()
    if not project_dir.is_dir():
        return False
    req_path = image_request_path(project_dir)
    if not req_path.is_file():
        return False
    request = load_image_request(req_path)
    return request_changes_project(project, request)


def _cmd_adapt_all(store: ConfigStore, args):
    """Apply pending image-request changes across all configured projects."""
    if bool(getattr(args, "show_prompt", False)):
        print("Error: --show-prompt cannot be used with --all.")
        sys.exit(1)
    if bool(getattr(args, "discover", False)):
        print("Error: --discover cannot be used with --all.")
        print("Run '--discover' per project instead.")
        sys.exit(1)
    if bool(getattr(args, "clear", False)):
        print("Error: --clear cannot be used with --all.")
        sys.exit(1)
    if bool(getattr(args, "write_only", False)):
        print("Error: --write-only cannot be used with --all.")
        sys.exit(1)
    if getattr(args, "base_image", "") or getattr(args, "from_image", ""):
        print("Error: --base-image/--from-image cannot be used with --all.")
        sys.exit(1)
    if list(getattr(args, "package", []) or []) or list(getattr(args, "extra_command", []) or []):
        print("Error: --package/--command cannot be used with --all.")
        sys.exit(1)

    project_names = store.list_resources("Project")
    projects = [(name, store.resolve_project(name)) for name in project_names]
    projects = [(name, project) for name, project in projects if project is not None]
    pending_names = [name for name, project in projects if _project_has_pending_request(project)]

    if not pending_names:
        print("No projects with pending image-request changes.")
        return

    print(f"Applying pending image-request changes for {len(pending_names)} project(s)...")
    success = 0
    failed = []
    build_all = bool(getattr(args, "build", False))

    for project_name in pending_names:
        print()
        print(f"[adapt --all] {project_name}")
        project_args = SimpleNamespace(
            name=project_name,
            all=False,
            show_prompt=False,
            discover=False,
            base_image="",
            from_image="",
            package=[],
            extra_command=[],
            apply_only=True,
            clear=False,
            write_only=False,
            build=build_all,
        )
        try:
            cmd_adapt(project_args)
            success += 1
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if code == 0:
                success += 1
            else:
                failed.append(project_name)

    print()
    print(f"Adapted {success}/{len(pending_names)} pending project(s).")
    if failed:
        print("Failed:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)


def _ensure_project_directory(store: ConfigStore, project) -> Path:
    """Ensure project.directory exists; clone repo when needed."""
    if project.repo:
        clone_dir = store.repo_dir(project.name)
        if not clone_dir.exists():
            print(f"Cloning {project.repo} into {clone_dir}...")
            clone_cmd = ["git", "clone"]
            if project.ssh.private_key:
                ssh_cmd = f"ssh -i {project.ssh.private_key} -o StrictHostKeyChecking=no"
                clone_cmd = ["git", "-c", f"core.sshCommand={ssh_cmd}", "clone"]
            clone_cmd += [project.repo, str(clone_dir)]
            try:
                subprocess.run(clone_cmd, check=True)
            except subprocess.CalledProcessError:
                print(f"Error: Failed to clone {project.repo}")
                sys.exit(1)
        project.directory = str(clone_dir)

    if not project.directory:
        return None

    p = Path(project.directory).expanduser().resolve()
    if not p.is_dir():
        print(f"Error: Project directory does not exist: {p}")
        sys.exit(1)
    return p


def _auth_files_for_agent(project, agent) -> list:
    auth_files = [Path(f).name for f in list(agent.auth.files or []) if str(f).strip()]
    if not auth_files:
        if project.agent == "codex":
            auth_files = ["auth.json"]
        elif project.agent == "claude":
            auth_files = [".credentials.json", ".claude.json"]
    return auth_files


def _sync_auth_from_host(data_dir: Path, cred, agent) -> int:
    """Sync persisted auth files from resolved host credential sources."""
    copied = 0
    for src, dest_name in resolve_credential_sources(cred, agent):
        safe_dest = Path(dest_name).name.strip()
        if not safe_dest:
            continue
        if src.is_file():
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, data_dir / safe_dest)
            copied += 1
    return copied


def _ensure_runtime_image(store: ConfigStore, project, sec, agent) -> str:
    """Return an existing project image name, building lazily when needed."""
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    image_name = image_name_for_project(image_name_base, project)
    if image_exists(image_name):
        print(f"[adapt] Runtime image ready: {image_name}")
        return image_name

    print(f"[adapt] Runtime image missing: {image_name}")
    print("[adapt] Building runtime image...")
    container_dir = store.get_container_dir()
    if container_dir is None:
        print("Error: Cannot find container build assets (entrypoint.sh).")
        print("Set toolDir in global.yaml or reinstall skua.")
        sys.exit(1)

    base_image = g.get("baseImage", "debian:bookworm-slim")
    defaults = g.get("defaults", {})
    build_security_name = defaults.get("security", "open")
    build_security = store.load_security(build_security_name) or sec
    image_config = g.get("image", {})
    global_extra_packages = image_config.get("extraPackages", [])
    global_extra_commands = image_config.get("extraCommands", [])
    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_extra_packages,
        global_extra_commands=global_extra_commands,
    )
    success = build_image(
        container_dir=container_dir,
        image_name=image_name,
        security=build_security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        quiet=True,
    )
    if not success:
        print(f"Error: failed to build image '{image_name}'.")
        sys.exit(1)
    print(f"[adapt] Runtime image built: {image_name}")
    return image_name


def _noninteractive_run_command(base_cmd: list, project_name: str, suffix: str) -> list:
    cmd = [token for token in base_cmd if token != "-it"]
    if "--name" in cmd:
        idx = cmd.index("--name")
        if idx + 1 < len(cmd):
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in project_name).strip("-_") or "project"
            cmd[idx + 1] = f"skua-{safe}-adapt-{suffix}-{os.getpid()}"
    return cmd


def _shell_join(argv: list) -> str:
    """Return shell-safe preview text for a command argv list."""
    return " ".join(shlex.quote(str(token)) for token in (argv or []))


def _agent_prompt(project_name: str, agent_name: str) -> str:
    return (
        f"You are adapting the project '{project_name}'. "
        "You are running inside the project's Docker container environment. "
        "Inspect the current repository and update only .skua/image-request.yaml. "
        "Infer dependencies by reading project files (for example README/docs, lockfiles, manifests, build scripts, CI config). "
        "Set status: ready and provide a short summary. "
        "Only request missing tools/dependencies and avoid listing packages that are already available. "
        "While you work, if a missing tool/system dependency blocks progress, immediately record it in .skua/image-request.yaml. "
        "Keep requests minimal and incremental. "
        "Use packages for apt dependencies, commands for setup steps, and optionally "
        "baseImage or fromImage when needed. "
        "Do not modify any other file."
    )


def _template_uses_shell(template: str) -> bool:
    """Return True when an adapt command template needs shell semantics."""
    t = template or ""
    shell_markers = ("\n", ";", "|", "&&", "||", ">", "<", "$(", "${", "`")
    return any(marker in t for marker in shell_markers)


def _normalize_adapt_argv(agent_name: str, argv: list) -> list:
    """Normalize agent argv for non-interactive adapt behavior."""
    if agent_name == "claude" and argv:
        has_prompt = "-p" in argv or "--print" in argv
        if argv[0] == "claude" and has_prompt and "--dangerously-skip-permissions" not in argv:
            return [argv[0], "--dangerously-skip-permissions", *argv[1:]]
    return argv


def _strip_ansi(text: str) -> str:
    """Return text with common ANSI escape sequences removed."""
    ansi_re = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_re.sub("", text or "")


def _is_entrypoint_noise(line: str) -> bool:
    """Return True when a line is startup noise from entrypoint."""
    s = line.strip()
    if not s:
        return True
    prefixes = (
        "============================================",
        "skua — Dockerized Coding Agent",
        "Agent:",
        "Auth:",
        "Credential:",
        "Project:",
        "Image adapt request:",
        "Adapt guide:",
        "Usage:",
        "tmux attach -t ",
        "tmux detach:",
        "claude-dsp",
        "claude -> Start",
        "[OK]",
        "[--]",
    )
    return s.startswith(prefixes)


def _summarize_agent_output(stdout: str, stderr: str) -> list:
    """Return filtered, compact agent output lines."""
    combined = "\n".join(part for part in [stdout, stderr] if part)
    out = []
    for raw in _strip_ansi(combined).splitlines():
        line = raw.strip()
        if _is_entrypoint_noise(line):
            continue
        out.append(line)

    deduped = []
    prev = None
    for line in out:
        if line == prev:
            continue
        deduped.append(line)
        prev = line
    return deduped[-12:]


def _request_preview_lines(request: dict) -> list:
    """Return concise preview lines for a generated wishlist request."""
    summary = str(request.get("summary", "") or "").strip() or "(none)"
    from_image = str(request.get("fromImage", "") or "").strip() or "(unchanged)"
    base_image = str(request.get("baseImage", "") or "").strip() or "(unchanged)"
    packages = [str(p).strip() for p in list(request.get("packages", []) or []) if str(p).strip()]
    commands = [str(c).strip() for c in list(request.get("commands", []) or []) if str(c).strip()]
    return [
        f"summary: {summary}",
        f"fromImage: {from_image}",
        f"baseImage: {base_image}",
        f"packages: {', '.join(packages) if packages else '(none)'}",
        f"commands: {len(commands)} command(s)",
    ]


def _is_interactive_tty() -> bool:
    """Return True when stdin/stdout are interactive terminal streams."""
    stdin = getattr(sys, "stdin", None)
    stdout = getattr(sys, "stdout", None)
    return bool(stdin and stdout and stdin.isatty() and stdout.isatty())


def _confirm_apply_wishlist(agent_name: str, request: dict) -> bool:
    """Prompt user to approve generated adaptations before applying."""
    print(f"[adapt] {agent_name} generated wishlist:")
    for line in _request_preview_lines(request):
        print(f"  - {line}")
    answer = input("Approve and apply these adaptations? [Y/n]: ").strip().lower()
    return answer != "n"


def _agent_adapt_command(agent, project_name: str) -> list:
    agent_name = (agent.name or "").strip().lower()
    prompt = _agent_prompt(project_name, agent_name)

    template = str(getattr(agent.runtime, "adapt_command", "") or "").strip()
    if template:
        rendered = template.format(
            prompt=prompt,
            prompt_shell=shlex.quote(prompt),
            project=project_name,
        )
        if _template_uses_shell(template):
            return ["bash", "-lc", rendered]
        try:
            return _normalize_adapt_argv(agent_name, shlex.split(rendered))
        except ValueError as exc:
            print(f"Error: Invalid adapt command for agent '{agent.name}': {exc}")
            sys.exit(1)

    runtime = (agent.runtime.command or "").strip()
    if runtime:
        base = shlex.split(runtime)
    else:
        base = [agent_name or "agent"]
    if agent_name == "codex":
        return base + ["exec", prompt]
    if agent_name == "claude":
        return _normalize_adapt_argv(agent_name, base + ["-p", prompt])
    print(f"Error: Automated adapt is unsupported for agent '{agent.name}'.")
    print("Use `skua adapt <project> --apply-only` after updating .skua/image-request.yaml manually.")
    sys.exit(1)


def _ensure_agent_authenticated(store: ConfigStore, project, env, agent, cred, docker_cmd_base: list):
    auth_files = _auth_files_for_agent(project, agent)
    if not auth_files:
        print(f"Error: No auth files configured for agent '{agent.name}'.")
        sys.exit(1)

    auth_dir = (agent.auth.dir or ".claude").lstrip("/")
    primary_auth = auth_files[0]

    if env.persistence.mode == "bind":
        data_dir = store.project_data_dir(project.name, project.agent)
        copied = _sync_auth_from_host(data_dir, cred, agent)
        if copied:
            print(f"Synced {copied} auth file(s) from host.")
        if not (data_dir / primary_auth).is_file():
            login_cmd = agent.auth.login_command or f"{agent.runtime.command or agent.name} login"
            print(f"Error: Agent '{agent.name}' is not logged in for project '{project.name}'.")
            print(f"Missing auth file: {data_dir / primary_auth}")
            print(f"Run 'skua run {project.name}' and execute '{login_cmd}', then retry.")
            sys.exit(1)

    check_cmd = _noninteractive_run_command(docker_cmd_base, project.name, "authcheck")
    check_cmd.extend(["bash", "-lc", f"test -f /home/dev/{auth_dir}/{primary_auth}"])
    result = subprocess.run(check_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        login_cmd = agent.auth.login_command or f"{agent.runtime.command or agent.name} login"
        print(f"Error: Agent '{agent.name}' is not logged in for project '{project.name}'.")
        print(f"Run 'skua run {project.name}' and execute '{login_cmd}', then retry.")
        sys.exit(1)


def _run_agent_adapt_session(store: ConfigStore, project, env, sec, agent):
    """Start an adapt container session and ask the agent to update image-request.yaml."""
    print("[adapt] Preparing runtime image...")
    image_name = _ensure_runtime_image(store, project, sec, agent)
    data_dir = store.project_data_dir(project.name, project.agent)

    docker_cmd_base = build_run_command(
        project=project,
        environment=env,
        security=sec,
        agent=agent,
        image_name=image_name,
        data_dir=data_dir,
    )
    cred = None
    if project.credential:
        cred = store.load_credential(project.credential)
        if cred is None:
            print(f"Warning: Credential '{project.credential}' not found; using default auth source.")
    print("[adapt] Syncing credentials and checking auth...")
    _ensure_agent_authenticated(store, project, env, agent, cred, docker_cmd_base)

    print(f"[adapt] {agent.name} is generating wishlist...")
    run_cmd = _noninteractive_run_command(docker_cmd_base, project.name, "agent")
    run_cmd.extend(_agent_adapt_command(agent, project.name))
    result = subprocess.run(run_cmd, capture_output=True, text=True)
    summary_lines = _summarize_agent_output(result.stdout, result.stderr)
    if summary_lines:
        print("[adapt] Agent output:")
        for line in summary_lines:
            print(f"  {line}")
    if result.returncode != 0:
        print("Error: Automated adapt agent session failed.")
        if not summary_lines:
            print("Last command output:")
            combined = "\n".join(
                part for part in [result.stderr, result.stdout] if part
            )
            lines = [line.rstrip() for line in _strip_ansi(combined).splitlines() if line.strip()]
            for line in lines[-12:]:
                print(f"  {line}")
        print("Re-run with '--apply-only' after updating .skua/image-request.yaml manually.")
        sys.exit(1)


def _request_from_flags(args):
    """Build an image request from CLI flags, or None when no request flags given."""
    has_flag_request = bool(
        args.base_image
        or args.from_image
        or args.package
        or args.extra_command
    )
    if not has_flag_request:
        return None
    return {
        "schemaVersion": 1,
        "status": "ready",
        "summary": "Applied from skua adapt CLI flags.",
        "baseImage": args.base_image or "",
        "fromImage": args.from_image or "",
        "packages": list(args.package or []),
        "commands": list(args.extra_command or []),
    }


def _print_project_image_summary(project):
    img = project.image
    print("Resolved image config:")
    print(f"  fromImage:    {img.from_image or '(none)'}")
    print(f"  baseImage:    {img.base_image or '(none)'}")
    print(f"  packages:     {', '.join(img.extra_packages) if img.extra_packages else '(none)'}")
    print(f"  commands:     {len(img.extra_commands)} command(s)")


def _build_project_image(store: ConfigStore, project, agent):
    """Build the adapted project image immediately."""
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    image_name = image_name_for_project(image_name_base, project)
    if image_exists(image_name):
        print(f"Image already exists: {image_name}")
        return

    container_dir = store.get_container_dir()
    if container_dir is None:
        print("Error: Cannot find container build assets (entrypoint.sh).")
        print("Set toolDir in global.yaml or reinstall skua.")
        sys.exit(1)

    base_image = g.get("baseImage", "debian:bookworm-slim")
    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])
    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_packages,
        global_extra_commands=global_commands,
    )
    defaults = g.get("defaults", {})
    build_security_name = defaults.get("security", "open")
    build_security = store.load_security(build_security_name)

    print(f"Building image: {image_name}")
    print(f"  Base image: {resolved_base_image}")
    if extra_packages:
        print(f"  Packages:   {', '.join(extra_packages)}")
    success = build_image(
        container_dir=container_dir,
        image_name=image_name,
        security=build_security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        quiet=True,
    )
    if not success:
        print(f"Error: failed to build image '{image_name}'.")
        sys.exit(1)
