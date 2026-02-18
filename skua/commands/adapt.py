# SPDX-License-Identifier: BUSL-1.1
"""skua adapt — apply project image requests from template/flags."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

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
    load_image_request,
    request_has_updates,
    apply_image_request_to_project,
    write_applied_image_request,
)


def cmd_adapt(args):
    store = ConfigStore()
    name = args.name

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
    automated_mode = (
        request_from_flags is None
        and not args.clear
        and not getattr(args, "apply_only", False)
    )

    if automated_mode:
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
        if automated_mode:
            print("Agent did not request any image customizations.")
        else:
            print("Ask your agent to update the request template, then run this command again.")
        return

    changed = apply_image_request_to_project(project, request)
    if not changed:
        print("Project image configuration already matches request; no changes applied.")
    else:
        store.save_resource(project)
        print(f"Applied image request from: {request_source}")
        print(f"Project image version: v{project.image.version}")
        _print_project_image_summary(project)
        if request_source != "flags":
            write_applied_image_request(request_path, request, project.image.version)

    should_build = bool(getattr(args, "build", False) or automated_mode)
    if should_build and changed:
        _build_project_image(store, project, agent)
    else:
        print(f"Next: run 'skua run {project.name}' to build/use the updated image.")


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


def _seed_auth_from_host(data_dir: Path, auth_dir: str, auth_files: list) -> int:
    """Seed missing persisted auth files from host HOME, matching `skua run` behavior."""
    copied = 0
    home = Path.home()
    rel_auth_dir = (auth_dir or ".claude").lstrip("/")
    codex_home = os.environ.get("CODEX_HOME", "").strip()

    for fname in auth_files or []:
        name = Path(fname).name
        if not name:
            continue

        dest = data_dir / name
        if dest.exists():
            continue

        candidates = [home / rel_auth_dir / name]
        if rel_auth_dir == ".codex" and codex_home:
            candidates.append(Path(codex_home).expanduser() / name)
        candidates.append(home / name)
        for src in candidates:
            if src.is_file():
                data_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied += 1
                break

    return copied


def _ensure_runtime_image(store: ConfigStore, project, sec, agent) -> str:
    """Return an existing project image name, building lazily when needed."""
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    image_name = image_name_for_project(image_name_base, project)
    if image_exists(image_name):
        return image_name

    print(f"Image '{image_name}' not found for agent '{project.agent}'.")
    print("Building image lazily...")
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
    )
    if not success:
        print(f"Error: failed to build image '{image_name}'.")
        sys.exit(1)
    return image_name


def _noninteractive_run_command(base_cmd: list, project_name: str, suffix: str) -> list:
    cmd = [token for token in base_cmd if token != "-it"]
    if "--name" in cmd:
        idx = cmd.index("--name")
        if idx + 1 < len(cmd):
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in project_name).strip("-_") or "project"
            cmd[idx + 1] = f"skua-{safe}-adapt-{suffix}-{os.getpid()}"
    return cmd


def _agent_prompt(project_name: str, agent_name: str) -> str:
    return (
        f"You are adapting the project '{project_name}'. "
        "Inspect the current repository and update only `.skua/image-request.yaml`. "
        "Set `status: ready` and provide a short `summary`. "
        "Use `packages` for apt dependencies, `commands` for setup steps, and optionally "
        "`baseImage` or `fromImage` when needed. "
        "Do not modify any other file."
    )


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
        return ["bash", "-lc", rendered]

    runtime = (agent.runtime.command or "").strip()
    if runtime:
        base = shlex.split(runtime)
    else:
        base = [agent_name or "agent"]
    if agent_name == "codex":
        return base + ["exec", prompt]
    if agent_name == "claude":
        return base + ["-p", prompt]
    print(f"Error: Automated adapt is unsupported for agent '{agent.name}'.")
    print("Use `skua adapt <project> --apply-only` after updating .skua/image-request.yaml manually.")
    sys.exit(1)


def _ensure_agent_authenticated(store: ConfigStore, project, env, agent, docker_cmd_base: list):
    auth_files = _auth_files_for_agent(project, agent)
    if not auth_files:
        print(f"Error: No auth files configured for agent '{agent.name}'.")
        sys.exit(1)

    auth_dir = (agent.auth.dir or ".claude").lstrip("/")
    primary_auth = auth_files[0]

    if env.persistence.mode == "bind":
        data_dir = store.project_data_dir(project.name, project.agent)
        copied = _seed_auth_from_host(data_dir, auth_dir, auth_files)
        if copied:
            print(f"Seeded {copied} auth file(s) from host.")
        if not (data_dir / primary_auth).is_file():
            login_cmd = agent.auth.login_command or f"{agent.runtime.command or agent.name} login"
            print(f"Error: Agent '{agent.name}' is not logged in for project '{project.name}'.")
            print(f"Missing auth file: {data_dir / primary_auth}")
            print(f"Run 'skua run {project.name}' and execute '{login_cmd}', then retry.")
            sys.exit(1)

    check_cmd = _noninteractive_run_command(docker_cmd_base, project.name, "authcheck")
    check_cmd.extend(["bash", "-lc", f"test -f /home/dev/{auth_dir}/{primary_auth}"])
    result = subprocess.run(check_cmd)
    if result.returncode != 0:
        login_cmd = agent.auth.login_command or f"{agent.runtime.command or agent.name} login"
        print(f"Error: Agent '{agent.name}' is not logged in for project '{project.name}'.")
        print(f"Run 'skua run {project.name}' and execute '{login_cmd}', then retry.")
        sys.exit(1)


def _run_agent_adapt_session(store: ConfigStore, project, env, sec, agent):
    """Start an adapt container session and ask the agent to update image-request.yaml."""
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
    _ensure_agent_authenticated(store, project, env, agent, docker_cmd_base)

    print(f"Starting automated adapt session for '{project.name}'...")
    run_cmd = _noninteractive_run_command(docker_cmd_base, project.name, "agent")
    run_cmd.extend(_agent_adapt_command(agent, project.name))
    result = subprocess.run(run_cmd)
    if result.returncode != 0:
        print("Error: Automated adapt agent session failed.")
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
    )
    if not success:
        print(f"Error: failed to build image '{image_name}'.")
        sys.exit(1)
