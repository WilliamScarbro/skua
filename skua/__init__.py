# SPDX-License-Identifier: BUSL-1.1
"""skua - Dockerized Coding Agent Manager"""

__version__ = "0.2.0"

from skua.tasks import (
    PromptExecution,
    TaskBrief,
    TaskMapping,
    TaskPlan,
    build_agent_prompt_command,
    compose_dispatch_prompt,
    dispatch_task_plan,
    ensure_project_running,
    load_task_brief,
    load_task_plan,
    make_task_plan,
    render_task_plan_text,
    resolve_task_projects,
    run_task_prompt,
)

__all__ = [
    "__version__",
    "PromptExecution",
    "TaskBrief",
    "TaskMapping",
    "TaskPlan",
    "build_agent_prompt_command",
    "compose_dispatch_prompt",
    "dispatch_task_plan",
    "ensure_project_running",
    "load_task_brief",
    "load_task_plan",
    "make_task_plan",
    "render_task_plan_text",
    "resolve_task_projects",
    "run_task_prompt",
]
