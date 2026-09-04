"""The domain-independent ReAct loop shared by both agents.

Part 1 completes the generic loop here; the two subclasses in this package
supply only their own tools and tool executors.
"""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI
import yaml

from assignment.env import Environment
from assignment.agent.tools import INVOKE_SKILL_TOOL

load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_COMPACTION_KEEP_RECENT_STEPS = 1
DEFAULT_COMPACTION_MAX_TOKENS = 1_200
MAX_OBSERVATION_CHARS = 10_000

COMPACTION_SYSTEM_PROMPT = """You create concise, factual working memory for a
software agent. Summarize only the supplied older conversation. Preserve the
objective and constraints; relevant files, symbols, and commands; edits already
made; concrete results and errors; failed approaches and why they failed; tests
and their outcomes; current blockers; and the next intended action. Do not copy
large raw outputs, invent facts, or include conversational filler. Return only
the working-memory summary."""


class StepLimitError(Exception):
    """Raised when an agent exhausts its model-call budget."""


def format_tool_output(output: dict[str, Any]) -> str:
    """Format a terminal result as a compact, tagged model observation."""

    elements: list[str] = []
    for key in sorted(output):
        value = output[key]
        if isinstance(value, str) and len(value) > MAX_OBSERVATION_CHARS:
            # Leave room for the elision notice so the formatted value itself,
            # not just its retained source slices, stays below the limit.
            retained_at_each_end = 4_900
            omitted = len(value) - (2 * retained_at_each_end)
            value = (
                f"{value[:retained_at_each_end]}\n"
                f"[{omitted} characters elided; read a narrower range]\n"
                f"{value[-retained_at_each_end:]}"
            )
        elements.append(f"<{key}>{value}</{key}>")
    return "\n".join(elements)


def rough_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate prompt tokens without a provider-specific tokenizer."""

    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return max(1, math.ceil(len(serialized) / 4))


class Agent:
    """Base class for a ReAct agent with pluggable tools."""

    def __init__(
        self,
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
        self.env = environment
        self.model = model or os.environ.get("OPENAI_MODEL")
        if not self.model:
            raise RuntimeError("OPENAI_MODEL is not set.")

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if not base_url:
            raise RuntimeError("OPENAI_BASE_URL is not set.")
        api_style = os.environ.get("OPENAI_API_STYLE", "").strip().lower()
        if not api_style:
            parsed_base_url = urlparse(base_url)
            api_style = (
                "ollama"
                if parsed_base_url.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed_base_url.port == 11434
                else "openai"
            )
        if api_style not in {"openai", "ollama"}:
            raise RuntimeError("OPENAI_API_STYLE must be 'openai' or 'ollama'.")
        self.api_style = api_style
        try:
            max_retries = int(os.environ.get("OPENAI_MAX_RETRIES", "5"))
        except ValueError as exc:
            raise RuntimeError("OPENAI_MAX_RETRIES must be an integer.") from exc
        if max_retries < 0:
            raise RuntimeError("OPENAI_MAX_RETRIES must be non-negative.")

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
        )

        self.logs_save_path = logs_save_path
        self.step_limit = step_limit
        self.auto_stop_environment = auto_stop_environment
        if compact_threshold_tokens is not None and compact_threshold_tokens <= 0:
            raise ValueError("compact_threshold_tokens must be positive or None")
        if (
            compaction_keep_recent_steps is not None
            and compaction_keep_recent_steps < 1
        ):
            raise ValueError("compaction_keep_recent_steps must be at least 1")
        if compaction_max_tokens is not None and compaction_max_tokens < 1:
            raise ValueError("compaction_max_tokens must be positive")
        # A None threshold turns compaction off. The other two settings then
        # describe a compaction that never happens, so fall back to the
        # defaults rather than leaving a None for later code to trip over.
        self.compact_threshold_tokens = compact_threshold_tokens
        self.compaction_keep_recent_steps = (
            DEFAULT_COMPACTION_KEEP_RECENT_STEPS
            if compaction_keep_recent_steps is None
            else compaction_keep_recent_steps
        )
        self.compaction_max_tokens = (
            DEFAULT_COMPACTION_MAX_TOKENS
            if compaction_max_tokens is None
            else compaction_max_tokens
        )

        # Each agent supplies its own opening messages: the standing
        # instructions, and the task statement that starts the run.
        self.system_prompt: str = ""
        self.task_prompt: str = ""

        self.api_prompts: list[list[dict[str, Any]]] = []
        self.api_responses: list[dict[str, Any]] = []
        self.compaction_events: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        # Subclasses may require a structured action on every turn. Most agents
        # leave this unset so the provider retains its default "auto" behavior.
        self.tool_choice: str | None = None
        self.finished = False
        self.steps_taken = 0

        self.skills_path = Path(skills_path) if skills_path is not None else None
        self.skills: dict[str, dict[str, str]] = (
            self.load_skills(self.skills_path) if self.skills_path is not None else {}
        )

        if self.skills:
            self.tools.append(INVOKE_SKILL_TOOL)

        # TODO(1.1.a): Add machinery to maintain agent state as it takes actions
        # and observes the results.
        # To support the ReAct loop, we will need to track the conversation history.
        # This will be implemented as a list of dictionaries, where each dictionary represents a message in the conversation.
        # Each message will have a role (e.g., "user", "assistant", "system") and content (the text of the message).
        # This will allow the agent to build prompts that include the entire conversation history, enabling it to make informed decisions based on past interactions.
        # Dictionary keys are strings such as "role", "content", and "tool_calls".
        # Values use "Any" because some values are strings, lists, nested dictionaries, or None
        # For example, it can look like this:
        # [
        #     {
        #         "role": "assistant",
        #         "content": "I will inspect the files.",
        #         "tool_calls": [...]
        #     },
        #     {
        #         "role": "tool",
        #         "tool_call_id": "call_1",
        #         "content": "README.md\nsrc\ntests"
        #     }
        # ]
        # As it is placed inside the constructor of the Agent class, it will be initialized as an empty list when an Agent instance is created.
        # This gives every agent instance its own independent history.
        self.conversation_history: list[dict[str, Any]] = []

    def load_skills(self, skills_path: Path) -> dict[str, dict[str, str]]:
        """Load the skill folders exposed to this agent."""

        if not skills_path.exists():
            raise ValueError(f"Skills path does not exist: {skills_path}")
        if not skills_path.is_dir():
            raise ValueError(f"Skills path is not a directory: {skills_path}")

        skills: dict[str, dict[str, str]] = {}
        skill_directories = sorted(
            (path for path in skills_path.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )

        for skill_directory in skill_directories:
            skill_file = skill_directory / "SKILL.md"
            if not skill_file.is_file():
                raise ValueError(
                    f"Skill directory is missing SKILL.md: {skill_directory}"
                )

            try:
                content = skill_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"Could not read skill file {skill_file}: {exc}") from exc

            lines = content.splitlines()
            if not lines or lines[0].strip() != "---":
                raise ValueError(
                    f"Skill file must start with YAML frontmatter: {skill_file}"
                )

            try:
                frontmatter_end = next(
                    index
                    for index, line in enumerate(lines[1:], start=1)
                    if line.strip() == "---"
                )
            except StopIteration as exc:
                raise ValueError(
                    f"Skill file has unclosed YAML frontmatter: {skill_file}"
                ) from exc

            try:
                frontmatter = yaml.safe_load("\n".join(lines[1:frontmatter_end]))
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"Skill file has malformed YAML frontmatter: {skill_file}"
                ) from exc

            if not isinstance(frontmatter, dict):
                raise ValueError(
                    f"Skill frontmatter must be a YAML mapping: {skill_file}"
                )

            name = frontmatter.get("name")
            description = frontmatter.get("description")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"Skill frontmatter needs a non-empty string name: {skill_file}"
                )
            if not isinstance(description, str) or not description.strip():
                raise ValueError(
                    "Skill frontmatter needs a non-empty string description: "
                    f"{skill_file}"
                )

            name = name.strip()
            description = description.strip()
            if name in skills:
                raise ValueError(f"Duplicate skill name: {name}")

            skills[name] = {
                "metadata": f"name: {name}\ndescription: {description}",
                "content": content,
            }

        return skills

    def query_language_model(self) -> dict[str, Any]:
        """Send one tool-enabled Chat Completions request and normalize it."""

        messages = self.build_prompt()
        self.api_prompts.append(deepcopy(messages))
        step_number = self.steps_taken + 1
        print(
            f"[agent] step {step_number}/{self.step_limit}: requesting action",
            flush=True,
        )
        try:
            request_options = (
                {"max_tokens": 4096}
                if self.api_style == "ollama"
                else {
                    "max_completion_tokens": 4096,
                    "reasoning_effort": "medium",
                }
            )
            if self.tool_choice is not None:
                request_options["tool_choice"] = self.tool_choice
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
                **request_options,
            )
        except Exception as exc:
            print(
                f"[agent] step {step_number}: model request failed after retries "
                f"({type(exc).__name__}: {exc})",
                flush=True,
            )
            raise
        self.api_responses.append(response.model_dump(mode="json"))
        self.steps_taken += 1
        message = self.process_response(response)
        tool_names = [
            call.get("function", {}).get("name", "unknown")
            for call in message.get("tool_calls", [])
            if isinstance(call, dict)
        ]
        if tool_names:
            print(
                f"[agent] step {step_number}: tool call(s): {', '.join(tool_names)}",
                flush=True,
            )
        else:
            print(
                f"[agent] step {step_number}: response contained no parsed tool call; "
                "the loop should preserve the response and continue",
                flush=True,
            )
        return message

    def process_response(self, response: Any) -> dict[str, Any]:
        """Return relevant parts of the language model's response."""

        return response.choices[0].message.model_dump(exclude_none=True)

    def build_prompt(self) -> list[dict[str, Any]]:
        # TODO(1.1.a): Construct a sequence of messages that form the language
        # model prompt. This should include standing instructions, task
        # specification, prior interaction including observations, reasoning,
        # and actions from previous turns. Note that this method should be
        # domain-agnostic and construct the prompt in a way that would apply
        # to any of the inheriting domain-specific agents.

        # You want to be careful about which attributes of the class you modify
        # here as they may also be handled by the subclasses.
        
        # What is deepcopy() versus shallowcopy() debate ? 
        # Say, original = [1, 2, 3] and another = original.
        # In this case, Python does not create a second list here. Both variables refer to the same list. 
        # So, another.append(4) and if we print(original), we will get original as [1, 2, 3, 4], eventhough we did not append to original. 
        # Hence, Changing the object through one references makes the change visible through every available reference. 
        # Here, both original and another are references to the same list object.
        # NOW, what is a deepcopy() ? 
        # A deepcopy recursively copies the outer container and its nested mutable objects. 
        # So, when we do independent = deepcopy(original), the structure is now conceptually as follows : 
        # original list
        #     └── original message dictionary
        #             └── original tool_calls list
        #                     └── original tool-call dictionary
        # independent list
        #     └── copied message dictionary
        #             └── copied tool_calls list
        #                     └── copied tool-call dictionary
        # Changing the copy no longer changes the original, as : 
        # independent[0]["tool_calls"][0]["id"] = "changed" 
        # print(original[0]["tool_calls"][0]["id"]) will still print call_1, not changed.
        # Why does build_prompt(self) need deepcopy() ? 
        # Because, the stored history belongs to the agent and build_prompt() gives another part of the program a constructed prompt.
        # So, if we had used *self.conversation_history instead of *deepcopy(self.conversation_history), 
        # the caller could change the returned prompt and corrupt the agent's stored conversation state, because
        # the * creates a new outer list, but the history dictionaries remain shared. This is effectively a shallow copy.
        # Without deepcopy(), the agent’s memory would be corrupted through the returned prompt.
        # Using *deepcopy(self.conversation_history) creates an independent snapshot. 
        # The model client, logging code, tests, or compaction logic can handle the returned prompt without being able to mutate the agent’s stored history accidentally.
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.task_prompt},
            # Messages contain nested mutable lists/dicts such as tool_calls.
            # deepcopy prevents a caller from changing those objects through
            # the returned prompt and corrupting our stored conversation state.
            *deepcopy(self.conversation_history),
        ]

    def estimate_active_prompt_tokens(self) -> int:
        """Estimate the next prompt, calibrated by the provider's latest usage."""

        current_prompt = self.build_prompt()
        rough_current = rough_message_tokens(current_prompt)
        if not self.api_prompts or not self.api_responses:
            return rough_current

        usage = self.api_responses[-1].get("usage") or {}
        actual_previous = usage.get("prompt_tokens")
        if not isinstance(actual_previous, int):
            return rough_current

        rough_previous = rough_message_tokens(self.api_prompts[-1])
        added_since_previous_request = max(0, rough_current - rough_previous)
        return actual_previous + added_since_previous_request

    @property
    def compaction_enabled(self) -> bool:
        """Whether this agent compacts its context at all."""

        return self.compact_threshold_tokens is not None

    def compact_context(self):
        """Replace parts of prompt with model-generated working memory. Changes the
        content that `build_prompt` emits."""

        assistant_indices = [
            index
            for index, message in enumerate(self.conversation_history)
            if message.get("role") == "assistant"
        ]
        if len(assistant_indices) <= self.compaction_keep_recent_steps:
            raise ValueError("Not enough completed steps to compact context.")

        retained_start = assistant_indices[-self.compaction_keep_recent_steps]
        old_history = deepcopy(self.conversation_history[:retained_start])
        recent_history = deepcopy(self.conversation_history[retained_start:])

        compaction_prompt = [
            {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Use the following original standing instructions and task "
                    "as context when summarizing the older interaction.\n\n"
                    f"<original_system_prompt>\n{self.system_prompt}\n"
                    "</original_system_prompt>\n\n"
                    f"<original_task>\n{self.task_prompt}\n</original_task>"
                ),
            },
            *old_history,
            {
                "role": "user",
                "content": "Return concise working memory for the interaction above.",
            },
        ]

        ### Do not modify this section ###
        request_options = (
            {"max_tokens": self.compaction_max_tokens}
            if self.api_style == "ollama"
            else {
                "max_completion_tokens": self.compaction_max_tokens,
                "reasoning_effort": "medium",
            }
        )
        compaction_response = self.client.chat.completions.create(
            model=self.model,
            messages=compaction_prompt,
            **request_options,
        )

        compaction_response_dump = compaction_response.model_dump(mode="json")
        ##################################

        summary = compaction_response.choices[0].message.content
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError("Compaction model returned an empty summary.")

        self.conversation_history = [
            {
                "role": "user",
                "content": f"<working_memory>\n{summary.strip()}\n</working_memory>",
            },
            *recent_history,
        ]

        return compaction_prompt, compaction_response_dump

    def maybe_compact_context(self) -> bool:
        """Compact before the next action request when the threshold is reached."""

        if not self.compaction_enabled:
            return False

        # Context too short to compact yet
        if self.estimate_active_prompt_tokens() < self.compact_threshold_tokens:
            return False

        prompt_before = deepcopy(self.build_prompt())

        # Not enough steps (each assistant turn corresponds to a step) to force
        # compaction yet
        if (
            len([m for m in prompt_before if m.get("role") == "assistant"])
            <= self.compaction_keep_recent_steps
        ):
            return False

        compaction_prompt, compaction_response = self.compact_context()
        prompt_after = deepcopy(self.build_prompt())
        self.compaction_events.append(
            {
                "step": self.steps_taken,
                "estimated_tokens_before": rough_message_tokens(prompt_before),
                "estimated_tokens_after": rough_message_tokens(prompt_after),
                "active_prompt_before": deepcopy(prompt_before),
                "compaction_prompt": compaction_prompt,
                "compaction_response": compaction_response,
            }
        )
        return True

    def run(self) -> None:
        """Run ReAct steps, always saving the trajectory and stopping Modal."""

        try:
            while not self.finished:
                if self.steps_taken >= self.step_limit:
                    raise StepLimitError(
                        f"Agent reached its step limit of {self.step_limit}."
                    )

                self.maybe_compact_context()

                assistant_message = self.query_language_model()
                self.conversation_history.append(deepcopy(assistant_message))

                tool_calls = assistant_message.get("tool_calls", [])
                if not isinstance(tool_calls, list):
                    tool_calls = []
                observations = self.execute_tool_calls(tool_calls)
                self.conversation_history.extend(deepcopy(observations))
        finally:
            # This block is provided infrastructure. Do not modify it: a
            # trajectory is required even when a run fails.
            if self.logs_save_path:
                path = Path(self.logs_save_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "prompts": self.api_prompts,
                            "responses": self.api_responses,
                            "compactions": self.compaction_events,
                        },
                        indent=2,
                    )
                )
            if self.auto_stop_environment:
                stop = getattr(self.env, "stop", None)
                if callable(stop):
                    stop()

    def execute_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Execute domain-specific calls and return linked tool observations."""

        # You do not need to implement anything here. This method is
        # domain-specific and implemented by the relevant subclasses
        raise NotImplementedError
