"""Tools for interacting with the user and organizing work: ask_user, update_plan, delegate_task."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from kimi_agent.tools.base import Tool, ToolContext, ToolError, tool

PLAN_STATUSES = ("pending", "in_progress", "done")
MAX_SUBAGENT_DEPTH = 2


@tool(category="internal", summarize=lambda a: str(a.get("question", "")))
def ask_user(ctx: ToolContext, question: str, choices: list[str] | None = None) -> str:
    """Ask the user a question and wait for the answer.

    Use only when you are blocked: requirements are ambiguous, a decision belongs to the user, or you need
    information you cannot find yourself (credentials, preferences). Don't ask for permission to use tools;
    the user is asked automatically when needed.

    Args:
        question: A clear, specific question.
        choices: Optional suggested answers; the user can also type something else.
    """
    if not ctx.interactive or ctx.ui is None:
        return ("The user is not available to answer (non-interactive mode). Make a reasonable assumption, "
                "state it clearly in your final answer, and continue.")
    answer = ctx.ui.ask_user(question, [str(c) for c in (choices or [])])
    answer = (answer or "").strip()
    return f"The user answered: {answer}" if answer else "The user gave no answer. Use your best judgment."


class PlanStep(TypedDict):
    step: str
    status: Literal["pending", "in_progress", "done"]


def normalize_plan(steps: Any) -> list[dict[str, str]]:
    if not isinstance(steps, list):
        raise ToolError("steps must be a list of {step, status} objects.")
    plan: list[dict[str, str]] = []
    for item in steps:
        if isinstance(item, str):
            item = {"step": item, "status": "pending"}
        if not isinstance(item, dict):
            raise ToolError("Each step must be an object with 'step' and 'status'.")
        text = str(item.get("step") or item.get("title") or item.get("description") or "").strip()
        if not text:
            continue
        status = str(item.get("status", "pending")).strip().lower().replace(" ", "_").replace("-", "_")
        status = {"completed": "done", "complete": "done", "in_progress": "in_progress", "active": "in_progress",
                  "doing": "in_progress", "todo": "pending", "not_started": "pending"}.get(status, status)
        if status not in PLAN_STATUSES:
            status = "pending"
        plan.append({"step": text, "status": status})
    return plan


def _plan_summary(args: dict[str, Any]) -> str:
    steps = args.get("steps") or []
    if not isinstance(steps, list):
        return ""
    done = sum(1 for s in steps if isinstance(s, dict) and s.get("status") in ("done", "completed"))
    return f"{done}/{len(steps)} done"


@tool(category="internal", summarize=_plan_summary)
def update_plan(ctx: ToolContext, steps: list[PlanStep], explanation: str | None = None) -> str:
    """Create or update the task plan: a checklist that is shown to the user.

    Use it for tasks with several steps. Always send the complete list. Keep exactly one step
    'in_progress' while working, and mark steps 'done' as soon as they are finished.

    Args:
        steps: The full list of steps, each with a short description and a status.
        explanation: Optional note about why the plan changed.
    """
    plan = normalize_plan(steps)
    ctx.plan[:] = plan
    if ctx.ui is not None:
        ctx.ui.show_plan(plan, explanation)
    done = sum(1 for s in plan if s["status"] == "done")
    return f"Plan updated ({done}/{len(plan)} done)."


@tool(category="internal", summarize=lambda a: str(a.get("task", "")))
def delegate_task(ctx: ToolContext, task: str, context: str | None = None) -> str:
    """Delegate a self-contained sub-task to a sub-agent and get back its final report.

    The sub-agent has a fresh context window and the same tools and permissions as you (except delegation).
    It cannot see this conversation, so describe the task completely and say exactly what to report back.
    Good for broad research, exploring large codebases or independent chunks of work, keeping your own
    context small. Sub-agents run one at a time.

    Args:
        task: Complete description of what to do and what to report back.
        context: Extra background the sub-agent needs (relevant paths, findings, constraints).
    """
    if ctx.spawn_subagent is None:
        raise ToolError("Sub-agents are not available.")
    if ctx.depth >= MAX_SUBAGENT_DEPTH:
        raise ToolError("Maximum sub-agent nesting depth reached; do the task yourself.")
    prompt = task.strip()
    if context and context.strip():
        prompt += "\n\n# Context\n" + context.strip()
    return ctx.spawn_subagent(prompt)


INTERACTION_TOOLS: list[Tool] = [ask_user, update_plan]
SUBAGENT_TOOLS: list[Tool] = [delegate_task]
