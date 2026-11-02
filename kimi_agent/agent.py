"""The agent loop: model calls, approvals, tool execution, context management and sub-agents."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .context import ContextManager
from .llm import ContextOverflowError, LLMError, LLMResponse, ToolCall, Usage
from .safety import ASK, DENY
from .tools.base import Image, ToolContext, ToolError, ToolResult, one_line, prepare_arguments, truncate_middle

log = logging.getLogger("kimi_agent.agent")

INTERRUPTED = "[Interrupted: the user stopped the agent before this tool call finished.]"
SKIPPED = "[Skipped: not executed because the user denied an earlier tool call in this step.]"
EMPTY_NUDGE = ("(Your last response was empty.) If the task is complete, reply with a brief summary for the user; "
               "otherwise continue working.")
STEP_LIMIT_NOTE = ("You have reached the maximum number of steps for this request. Do not call any more tools. "
                   "Reply now with a summary of what you accomplished, what is unfinished and the next steps.")
MAX_IMAGES_PER_STEP = 4

PromptBuilder = Callable[[ToolContext, Any, bool], str]


@dataclass
class Outcome:
    output: str
    images: list[Image] = field(default_factory=list)
    stop: bool = False


class Agent:
    def __init__(self, *, llm: Any, registry: Any, ctx: ToolContext, policy: Any, ui: Any, settings: Any,
                 make_prompt: PromptBuilder, context: ContextManager | None = None, subagent: bool = False,
                 before_step: Callable[[], None] | None = None) -> None:
        self.llm = llm
        self.registry = registry
        self.ctx = ctx
        self.policy = policy
        self.ui = ui
        self.settings = settings
        self.make_prompt = make_prompt
        self.context = context or ContextManager(settings, llm)
        self.subagent = subagent
        self.before_step = before_step  # called before every model call, e.g. to refresh changed MCP tools
        self.messages: list[dict[str, Any]] = []
        self.usage = Usage()
        self.last_usage: Usage | None = None
        self.recent_calls: deque[str] = deque(maxlen=20)
        self._window_warned = False
        ctx.ui = ui
        ctx.spawn_subagent = self._spawn_subagent if "delegate_task" in registry else None
        self.refresh_system_prompt()

    # -- conversation management
    def refresh_system_prompt(self) -> None:
        prompt = self.make_prompt(self.ctx, self.registry, self.subagent)
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def reset(self) -> None:
        self.messages = self.messages[:1]
        self.recent_calls.clear()
        self.ctx.plan.clear()
        self.refresh_system_prompt()

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = [m for m in messages if m.get("role") != "system"]
        self.refresh_system_prompt()
        self._repair_tool_results()

    def pop_turn(self) -> dict[str, Any] | None:
        """Remove the last user turn and everything after it. Returns that turn's user message."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m.get("role") == "user" and m.get("_turn"):
                del self.messages[i:]
                self.recent_calls.clear()
                return m
        return None

    def context_tokens(self) -> int:
        return self.context.estimate(self.messages, self.registry.schemas())

    def compact(self, focus: str = "") -> bool:
        return self._maybe_compact(force=True, focus=focus)

    # -- running
    def run(self, content: Any, meta: dict[str, Any] | None = None) -> str:
        """Run one user request to completion and return the final answer.

        ``meta`` adds private keys (``_``-prefixed, never sent to the model) to the user message, e.g. the
        text a GUI displayed for it.
        """
        self._append_user(content, turn=True)
        if meta:
            self.messages[-1].update({k: v for k, v in meta.items() if k.startswith("_")})
        if not self.subagent and self.ctx.file_history is not None:
            self.ctx.file_history.begin_turn()
            self.messages[-1]["_file_turn"] = self.ctx.file_history.turn
        self._check_window()
        return self._loop()

    def _check_window(self) -> None:
        if self._window_warned or self.subagent:
            return
        fixed = self.context.fixed_tokens(self.messages, self.registry.schemas())
        if fixed > self.context.threshold * 0.6:
            self._window_warned = True
            self.ui.warn(f"The system prompt and tool definitions use ~{fixed:,} of the model's "
                         f"~{self.context.window:,}-token context window, leaving little room for the conversation. "
                         "Raise context_window (and the server's limit, e.g. OLLAMA_CONTEXT_LENGTH for Ollama) or "
                         "turn off unneeded tools with disabled_tools.")

    def _append_user(self, content: Any, turn: bool = False) -> None:
        last = self.messages[-1] if self.messages else None
        if last is not None and last.get("role") == "user" and not last.get("_summary"):
            old = last.get("content")
            if isinstance(old, str) and isinstance(content, str):
                last["content"] = f"{old}\n\n{content}" if old else content
            else:
                as_parts = (lambda c: list(c) if isinstance(c, list) else [{"type": "text", "text": str(c or "")}])
                last["content"] = as_parts(old) + as_parts(content)
            if turn:
                last["_turn"] = True
            return
        message: dict[str, Any] = {"role": "user", "content": content}
        if turn:
            message["_turn"] = True
        self.messages.append(message)

    def _loop(self) -> str:
        steps = 0
        nudged = False
        try:
            while True:
                if steps >= self.settings.max_steps:
                    if self.ctx.interactive and not self.subagent and self.ui.confirm(
                            f"The agent has taken {steps} steps on this request. Continue?", default=True):
                        steps = 0
                    else:
                        return self._step_limit_reached()
                steps += 1
                if self.before_step is not None:
                    self.before_step()
                self._maybe_compact()
                response = self._call_model()
                if not response.tool_calls:
                    if not response.content.strip() and not nudged:
                        nudged = True
                        self._append_user(EMPTY_NUDGE)
                        continue
                    self.messages.append(response.assistant_message())
                    self.ui.assistant_message(response.content, final=True)
                    if response.finish_reason == "length":
                        self.ui.warn("The answer was cut off by the output token limit (max_tokens).")
                    return response.content
                self.messages.append(response.assistant_message())
                self.ui.assistant_message(response.content, final=False)
                if self._run_tools(response):
                    return ""
        except KeyboardInterrupt:
            self.ui.end_response()
            self._repair_tool_results()
            raise

    def _call_model(self, schemas: list[dict[str, Any]] | None = None) -> LLMResponse:
        schemas = self.registry.schemas() if schemas is None else schemas
        overflowed = False
        while True:
            self.ui.begin_response()
            try:
                response = self.llm.complete(self.messages, schemas, on_content=self.ui.stream_content,
                                             on_reasoning=self.ui.stream_reasoning, on_retry=self.ui.start_spinner)
            except ContextOverflowError:
                self.ui.end_response()
                if overflowed:
                    raise
                overflowed = True
                self.context.on_overflow(self.messages, schemas)
                self.ui.warn("The conversation does not fit in the model's context window; compacting…")
                self._maybe_compact(force=True)
                continue
            finally:
                self.ui.end_response()
                for notice in self.llm.drain_notices():
                    self.ui.warn(notice)
            break
        self.context.calibrate(self.messages, schemas, response.usage)
        if response.usage is not None:
            self.usage.add(response.usage)
            self.last_usage = response.usage
        return response

    def _step_limit_reached(self) -> str:
        self.ui.warn(f"Step limit reached ({self.settings.max_steps} steps); asking the model to wrap up.")
        self._append_user(STEP_LIMIT_NOTE)
        try:
            response = self._call_model()
        except LLMError as exc:
            log.warning("final summary failed: %s", exc)
            response = LLMResponse()
        content = response.content or "Stopped: the step limit was reached before the task was finished."
        self.messages.append({"role": "assistant", "content": content})
        self.ui.assistant_message(content, final=True)
        return content

    def _plan_note(self) -> str:
        if not self.ctx.plan:
            return ""
        lines = [f"- [{step.get('status', 'pending')}] {step.get('step', '')}" for step in self.ctx.plan]
        return "Current plan:\n" + "\n".join(lines)

    def _maybe_compact(self, force: bool = False, focus: str = "") -> bool:
        schemas = self.registry.schemas()
        before = self.context.estimate(self.messages, schemas)
        try:
            new = self.context.maybe_compact(self.messages, schemas, force=force, focus=focus,
                                             extra=self._plan_note(), on_status=self.ui.start_spinner)
        finally:
            self.ui.stop_spinner()
        if new is None:
            return False
        self.messages[:] = new
        after = self.context.estimate(self.messages, schemas)
        self.ui.info(f"Context compacted: ~{before:,} → ~{after:,} tokens")
        return True

    # -- tools
    def _run_tools(self, response: LLMResponse) -> bool:
        """Execute the response's tool calls in order. Returns True when the user stopped the turn."""
        images: list[Image] = []
        calls = response.tool_calls
        for index, call in enumerate(calls):
            outcome = self._execute(call, response.finish_reason)
            self.messages.append(self._tool_message(call, outcome.output))
            images.extend(outcome.images)
            if outcome.stop:
                for rest in calls[index + 1:]:
                    self.messages.append(self._tool_message(rest, SKIPPED))
                self._add_images(images)
                return True
        self._add_images(images)
        return False

    @staticmethod
    def _tool_message(call: ToolCall, output: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output or "(no output)"}

    def _add_images(self, images: list[Image]) -> None:
        if not images:
            return
        parts: list[dict[str, Any]] = [{"type": "text", "text": "Image output of the tool call(s) above:"}]
        parts += [{"type": "image_url", "image_url": {"url": img.data_url()}} for img in images[-MAX_IMAGES_PER_STEP:]]
        self.messages.append({"role": "user", "content": parts})

    def _fail(self, tool: Any, message: str, args: dict[str, Any] | None = None, summary: str = "") -> Outcome:
        if tool is not None:
            self.ui.tool_call(tool, args or {}, summary)
            self.ui.tool_result(tool, ToolResult(message, is_error=True), 0.0)
        return Outcome(message)

    def _execute(self, call: ToolCall, finish_reason: str | None) -> Outcome:
        tool = self.registry.get(call.name)
        if tool is None:
            self.ui.warn(f"The model called an unknown tool: {call.name}")
            return Outcome(f"Error: there is no tool named '{call.name}'. "
                           f"Available tools: {', '.join(self.registry.names())}.")
        if call.parse_error is not None:
            message = (f"Error: the arguments for {call.name} are not valid JSON ({call.parse_error}). "
                       f"Received: {truncate_middle(call.raw_arguments or '', 400)}")
            if finish_reason == "length":
                message += (" Your response was cut off because it reached the output token limit. Make smaller "
                            "tool calls, e.g. write a large file in several parts.")
            return self._fail(tool, message, summary="(invalid arguments)")
        try:
            raw_args = json.loads(call.arguments) if call.arguments.strip() else {}
            args = prepare_arguments(tool, raw_args)
        except (ToolError, ValueError) as exc:
            return self._fail(tool, f"Error: {exc}", summary="(invalid arguments)")

        summary = tool.summary(args)
        self.ui.tool_call(tool, args, summary)
        decision = self.policy.evaluate(tool, args, self.ctx)
        if decision.action == DENY:
            message = (f"Blocked by the user's safety configuration: {decision.reason}. "
                       "Do not try to work around this; choose another approach or explain the limitation.")
            self.ui.tool_result(tool, ToolResult(message, is_error=True), 0.0)
            return Outcome(message)
        if decision.action == ASK:
            if not self.ctx.interactive:
                message = (f"Not permitted: {tool.name} needs the user's approval here ({decision.reason}), but the "
                           "agent is running non-interactively so nobody can approve it. Use another approach that "
                           "does not need this action, or finish and tell the user what to run themselves.")
                self.ui.tool_result(tool, ToolResult(message, is_error=True), 0.0)
                return Outcome(message)
            answer, feedback = self.ui.confirm_tool(tool, args, decision, self._preview(tool, args))
            if answer == "always":
                self.policy.remember(tool, decision)
            elif answer == "no":
                if feedback:
                    return Outcome(f"The user denied this tool call and said: {feedback}")
                self.ui.warn("Denied. Tell the agent what to do instead.")
                return Outcome("The user denied this tool call. Wait for the user's instructions.", stop=True)

        start = time.monotonic()
        try:
            with self.ui.tool_running(tool, summary):
                result = tool.run(self.ctx, args)
        except ToolError as exc:
            result = ToolResult(f"Error: {exc}", is_error=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.exception("tool %s crashed", tool.name)
            result = ToolResult(f"Error: {tool.name} failed unexpectedly: {type(exc).__name__}: {exc}",
                                is_error=True)
        elapsed = time.monotonic() - start
        self.ui.tool_result(tool, result, elapsed)
        output = truncate_middle(result.output or "", self.settings.max_output_chars)
        output += self._repetition_hint(call.name, args)
        images = result.images if (self.ctx.vision and self.llm.images_ok) else []
        return Outcome(output, list(images))

    def _preview(self, tool: Any, args: dict[str, Any]) -> tuple[str, str] | None:
        if tool.preview is None:
            return None
        try:
            return tool.preview(self.ctx, args)
        except Exception as exc:
            log.debug("preview for %s failed: %s", tool.name, exc)
            return None

    def _repetition_hint(self, name: str, args: dict[str, Any]) -> str:
        key = name + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        count = sum(1 for k in self.recent_calls if k == key)
        self.recent_calls.append(key)
        if count >= 2 and name not in ("process_output", "screenshot"):
            return (f"\n\n[Note: you have made this exact {name} call {count + 1} times recently. If it is not "
                    "giving you what you need, change your approach instead of repeating it.]")
        return ""

    def _repair_tool_results(self) -> None:
        """Give every tool call of the last assistant message a result (after an interruption)."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m.get("role") == "assistant":
                answered = {x.get("tool_call_id") for x in self.messages[i + 1:] if x.get("role") == "tool"}
                for tc in m.get("tool_calls") or []:
                    if tc.get("id") not in answered:
                        self.messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                              "name": tc.get("function", {}).get("name", ""),
                                              "content": INTERRUPTED})
                return
            if m.get("role") == "user" and m.get("_turn"):
                return

    # -- sub-agents
    def _spawn_subagent(self, task: str) -> str:
        child_ctx = self.ctx.fork()
        registry = self.registry.without("ask_user", "delegate_task")
        child_ui = self.ui.child("  │ ")
        child = Agent(llm=self.llm, registry=registry, ctx=child_ctx, policy=self.policy, ui=child_ui,
                      settings=self.settings, make_prompt=self.make_prompt, subagent=True)
        self.ui.info(f"Sub-agent started: {one_line(task.splitlines()[0] if task else '', 100)}")
        try:
            result = child.run(task)
        finally:
            self.usage.add(child.usage)
        self.ui.info("Sub-agent finished.")
        return result or "(The sub-agent finished without a report.)"
