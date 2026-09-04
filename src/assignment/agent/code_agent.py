"""The Part 1 coding agent: fix a software issue and submit a git patch."""

from __future__ import annotations

import json
from typing import Any

from assignment.agent.base import (
    DEFAULT_COMPACTION_KEEP_RECENT_STEPS,
    DEFAULT_COMPACTION_MAX_TOKENS,
    Agent,
    format_tool_output,
)
from assignment.agent.tools import EXECUTE_TOOL, SEND_MESSAGE_TOOL
from assignment.env import Environment


class CodeAgent(Agent):
    """An agent that fixes a software issue and submits a git patch."""

    def __init__(
        self,
        task: str,
        environment: Environment,
        model: str | None = None,
        logs_save_path: str | None = None,
        step_limit: int = 100,
        skills_path: str | None = None,
        auto_stop_environment: bool = True,
        compact_threshold_tokens: int | None = None,
        compaction_keep_recent_steps: int = DEFAULT_COMPACTION_KEEP_RECENT_STEPS,
        compaction_max_tokens: int = DEFAULT_COMPACTION_MAX_TOKENS,
    ):
        super().__init__(
            environment=environment,
            model=model,
            logs_save_path=logs_save_path,
            step_limit=step_limit,
            skills_path=skills_path,
            auto_stop_environment=auto_stop_environment,
            compact_threshold_tokens=compact_threshold_tokens,
            compaction_keep_recent_steps=compaction_keep_recent_steps,
            compaction_max_tokens=compaction_max_tokens,
        )
        self.task = task
        self.submitted_patch = ""
        self.invoked_skills: set[str] = set()

        self.tools.extend([EXECUTE_TOOL, SEND_MESSAGE_TOOL])
        # Small Ollama models are prone to printing a proposed tool call as
        # prose. A coding turn without an action cannot change or inspect the
        # sandbox, so require structured calls for the local provider.
        if self.api_style == "ollama":
            self.tool_choice = "required"

        system_information = json.dumps(
            {
                "machine": environment.machine,
                "release": environment.release,
                "system": environment.system,
                "version": environment.version,
            },
            indent=2,
        )
        self.system_prompt = (
            "You are a software engineering agent working in a sandboxed "
            "repository. Use the available tools to inspect the repository, "
            "implement the requested fix, and verify it with relevant tests. "
            "Base your conclusions on tool observations. When the task is "
            "complete, call `send_message` with a concise summary.\n\n"
            f"<system_information>\n{system_information}\n</system_information>"
        )
        self.task_prompt = task

        if self.skills:
            catalog = "\n".join(skill["metadata"] for skill in self.skills.values())
            self.system_prompt += (
                "\n\nReusable skills are available. Call `invoke_skill` with a "
                "skill's name to load its instructions, and follow them in place "
                f"of your default approach.\n\n<skills>\n{catalog}\n</skills>\n"
            )

    def execute_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Execute ``execute`` and ``send_message`` calls in the code sandbox."""

        observations: list[dict[str, str]] = []

        def add_observation(call_id: str, content: str) -> None:
            observations.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                }
            )

        def tool_error(message: str) -> str:
            return f"<tool_error>{message}</tool_error>"

        for call in tool_calls:
            if not isinstance(call, dict):
                add_observation("unknown", tool_error("Malformed tool call."))
                continue
            call_id = str(call.get("id", "unknown"))
            function = call.get("function")
            if not isinstance(function, dict):
                add_observation(call_id, tool_error("Malformed tool call."))
                continue

            name = function.get("name")
            raw_arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                add_observation(
                    call_id,
                    tool_error("Tool name and arguments must be strings."),
                )
                continue

            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                add_observation(
                    call_id,
                    tool_error(f"Arguments are not valid JSON: {exc.msg}."),
                )
                continue

            if not isinstance(arguments, dict):
                add_observation(
                    call_id,
                    tool_error("Tool arguments must be a JSON object."),
                )
                continue

            if name == "execute":
                allowed = {"command", "shell", "cwd", "timeout", "env"}
                extra = set(arguments) - allowed
                command = arguments.get("command")
                valid_command = isinstance(command, str) or (
                    isinstance(command, list)
                    and all(isinstance(part, str) for part in command)
                )
                shell = arguments.get("shell")
                cwd = arguments.get("cwd")
                timeout = arguments.get("timeout")
                env = arguments.get("env")
                valid_env = env is None or (
                    isinstance(env, dict)
                    and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in env.items()
                    )
                )

                if extra:
                    add_observation(
                        call_id,
                        tool_error(
                            "Unknown execute argument(s): "
                            + ", ".join(sorted(extra))
                            + "."
                        ),
                    )
                elif "command" not in arguments or not valid_command:
                    add_observation(
                        call_id,
                        tool_error("execute requires command to be a string or string list."),
                    )
                elif shell is not None and not isinstance(shell, bool):
                    add_observation(
                        call_id,
                        tool_error("execute shell must be a boolean or null."),
                    )
                elif cwd is not None and not isinstance(cwd, str):
                    add_observation(
                        call_id,
                        tool_error("execute cwd must be a string or null."),
                    )
                elif timeout is not None and (
                    isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                ):
                    add_observation(
                        call_id,
                        tool_error("execute timeout must be a number or null."),
                    )
                elif not valid_env:
                    add_observation(
                        call_id,
                        tool_error("execute env must map strings to strings or be null."),
                    )
                else:
                    result = self.env.execute(
                        command,
                        shell=shell,
                        cwd=cwd,
                        timeout=timeout,
                        env=env,
                    )
                    add_observation(call_id, format_tool_output(result))

            elif name == "send_message":
                if set(arguments) != {"summary"} or not isinstance(
                    arguments.get("summary"), str
                ):
                    add_observation(
                        call_id,
                        tool_error("send_message requires exactly one string summary."),
                    )
                    continue

                # A trajectory is saved even when a run fails, but downstream
                # evaluation needs the separate patch artifact. Prevent a model
                # from declaring success before following the submission skill.
                if "submit-task" in self.skills:
                    if "submit-task" not in self.invoked_skills:
                        add_observation(
                            call_id,
                            tool_error(
                                "Submission is incomplete. Invoke the "
                                "submit-task skill before sending the summary."
                            ),
                        )
                        continue
                    patch_check = self.env.execute("test -s patch.txt")
                    if patch_check["returncode"] != 0:
                        add_observation(
                            call_id,
                            tool_error(
                                "Submission is incomplete. Follow the submit-task "
                                "skill and create a non-empty patch.txt first."
                            ),
                        )
                        continue

                summary = arguments["summary"]
                self.finished = True
                add_observation(call_id, summary)

            elif name == "invoke_skill" and self.skills:
                if set(arguments) != {"name"} or not isinstance(
                    arguments.get("name"), str
                ):
                    add_observation(
                        call_id,
                        tool_error("invoke_skill requires exactly one string name."),
                    )
                elif arguments["name"] not in self.skills:
                    add_observation(
                        call_id,
                        tool_error(f"Unknown skill: {arguments['name']}."),
                    )
                else:
                    self.invoked_skills.add(arguments["name"])
                    add_observation(
                        call_id,
                        self.skills[arguments["name"]]["content"],
                    )

            else:
                add_observation(call_id, tool_error(f"Unknown tool: {name}."))

        return observations
