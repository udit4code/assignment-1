"""The Part 1 coding agent: fix a software issue and submit a git patch."""

from __future__ import annotations

import base64
from enum import Enum
import hashlib
import json
import posixpath
import re
from pathlib import PurePosixPath
from typing import Any

from assignment.agent.base import (
    DEFAULT_COMPACTION_KEEP_RECENT_STEPS,
    DEFAULT_COMPACTION_MAX_TOKENS,
    Agent,
    format_tool_output,
)
from assignment.agent.tools import (
    CODE_CAPABILITY_TOOLS,
    EXECUTE_TOOL,
    SEND_MESSAGE_TOOL,
)
from assignment.env import Environment


class CodeAgentPhase(str, Enum):
    """The next kind of work the coding agent is expected to perform."""

    DISCOVER = "discover"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    SUBMIT = "submit"


class ExecuteKind(str, Enum):
    """A conservative classification used to enforce phase transitions."""

    INSPECT = "inspect"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    SUBMIT = "submit"


class CodeAgent(Agent):
    """An agent that fixes a software issue and submits a git patch."""

    DEFAULT_DISCOVERY_INSPECTION_LIMIT = 6
    DEFAULT_PHASE_INSPECTION_LIMIT = 2
    TOOL_INTERFACES = ("capability", "legacy")
    MAX_EDIT_FAILURES = 3
    INSPECTION_BUDGET_ERROR = (
        "Inspection budget exhausted. The root cause and edit location should now be\n"
        "known. Make a source or test change, or run a focused verification command."
    )

    _VERIFY_PATTERNS = (
        re.compile(r"(?:^|[;&|]\s*)(?:python\s+-m\s+)?pytest(?:\s|$)"),
        re.compile(r"(?:^|[;&|]\s*)(?:py\.test|tox|nox)(?:\s|$)"),
        re.compile(
            r"(?:^|[;&|]\s*)(?:python\s+)?(?:\S*/)?runtests\.py(?:\s|$)"
        ),
        re.compile(r"(?:^|[;&|]\s*)python\s+manage\.py\s+test(?:\s|$)"),
        re.compile(r"(?:^|[;&|]\s*)python\s+-m\s+unittest(?:\s|$)"),
        re.compile(
            r"(?:^|[;&|]\s*)(?:make|npm|pnpm|yarn|cargo|go)\s+"
            r"(?:test|check)(?:\s|$)"
        ),
        # Executing a Python file is commonly a focused reproduction script.
        re.compile(
            r"(?:^|[;&|]\s*)python(?:\d+(?:\.\d+)?)?\s+"
            r"(?!-[c-])\S+\.py(?:\s|$)"
        ),
    )
    _INSPECTION_PATTERN = re.compile(
        r"^(?:"
        r"pwd|ls|find|fd|rg|grep|egrep|fgrep|cat|head|tail|nl|sed|awk|less|more|"
        r"stat|file|wc|which|whereis|type|env|printenv|tree|"
        r"git\s+(?:status|diff|show|log|grep|rev-parse|branch|ls-files)"
        r")(?:\s|$)"
    )
    _MUTATION_PATTERN = re.compile(
        r"^(?:"
        r"apply_patch|patch|tee|touch|mkdir|rm|mv|cp|install|chmod|chown|"
        r"git\s+apply|"
        r"(?:npm|pnpm|yarn|pip|uv|poetry|cargo)\s+(?:add|install|remove)"
        r")(?:\s|$)"
    )
    _FORBIDDEN_GIT_PATTERN = re.compile(
        r"(?:^|[;&|()\n]\s*)"
        r"(?:(?:command|sudo)\s+)?(?:env\s+)?"
        r"(?:[a-z_]\w*=\S+\s+)*git\s+"
        r"(?:(?:-[Cc]\s+\S+|"
        r"--(?:git-dir|work-tree)(?:=\S+|\s+\S+)|"
        r"--(?:no-pager|bare|literal-pathspecs|no-optional-locks))\s+)*"
        r"(?:add|commit|reset|restore|checkout|switch|stash|clean|rm|mv|am|"
        r"merge|rebase|cherry-pick)(?=\s|$|[;&|)])",
        re.IGNORECASE,
    )

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
        discovery_inspection_limit: int = DEFAULT_DISCOVERY_INSPECTION_LIMIT,
        phase_inspection_limit: int = DEFAULT_PHASE_INSPECTION_LIMIT,
        tool_interface: str = "legacy",
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
        self.execute_call_counts: dict[str, int] = {}
        self.edit_call_counts: dict[str, int] = {}
        if tool_interface not in self.TOOL_INTERFACES:
            raise ValueError(
                "tool_interface must be one of: " + ", ".join(self.TOOL_INTERFACES)
            )
        self.tool_interface = tool_interface
        if (
            isinstance(discovery_inspection_limit, bool)
            or discovery_inspection_limit < 1
        ):
            raise ValueError("discovery_inspection_limit must be at least 1")
        if isinstance(phase_inspection_limit, bool) or phase_inspection_limit < 1:
            raise ValueError("phase_inspection_limit must be at least 1")
        self.discovery_inspection_limit = discovery_inspection_limit
        self.phase_inspection_limit = phase_inspection_limit
        self.phase = CodeAgentPhase.DISCOVER
        self.phase_inspections = 0
        self.total_inspections = 0
        self.implementation_attempts = 0
        self.verification_attempts = 0
        self.verification_status = "not_run"
        self.has_successful_implementation = False
        self.patch_created = False
        self.patch_reviewed = False
        self.change_revision = 0
        self.verified_revision: int | None = None
        self.change_digest: str | None = None
        self.verified_digest: str | None = None
        self.applied_paths: set[str] = set()
        self.initial_revision: str | None = None
        self.initial_revision_checked = False
        self.worktree_diff_digest: str | None = None
        self.worktree_diff_status = "unknown"
        self.exposed_skills = dict(self.skills)
        if tool_interface == "capability":
            # The harness-owned submit capability supersedes the legacy
            # file-based submission skill.
            self.exposed_skills.pop("submit-task", None)
            if not self.exposed_skills:
                self.tools = [
                    tool
                    for tool in self.tools
                    if tool.get("function", {}).get("name") != "invoke_skill"
                ]

        self.tools.extend(
            CODE_CAPABILITY_TOOLS
            if tool_interface == "capability"
            else [EXECUTE_TOOL, SEND_MESSAGE_TOOL]
        )
        # Every coding turn must take an observable action. This avoids spending
        # a full response budget on prose that cannot advance the repository.
        self.tool_choice = "required"
        self.reasoning_effort = "low"
        self.compaction_reasoning_effort = "low"

        system_information = json.dumps(
            {
                "machine": environment.machine,
                "release": environment.release,
                "system": environment.system,
                "version": environment.version,
            },
            indent=2,
        )
        common_prompt = (
            "You are a software engineering agent working in a sandboxed "
            "repository. Use the available tools to inspect the repository, "
            "implement the requested fix, and verify it with relevant tests. "
            "Base your conclusions on tool observations. Maintain forward "
            "progress: do not repeat an inspection when its relevant result is "
            "already present. Once the root cause and edit location are known, "
            "implement the smallest reasonable change and run focused tests. "
            "Never claim that an edit was made or a test passed unless a tool "
            "observation confirms it. Follow the phase shown in every "
            "<agent_state> observation. Rejected phase-gate calls do not run, "
            "so follow their next_action instead of retrying them. "
            "Never stage or commit repository changes. Do not run git add, git "
            "commit, git reset, git restore, git checkout, git switch, git "
            "stash, git clean, or other Git history-changing commands. Keep "
            "source edits as uncommitted working-tree changes so submission can "
            "capture them. "
            f"This run has a hard budget of {step_limit} action steps. Reserve enough steps "
            "for implementation, verification, and submission."
        )
        if tool_interface == "capability":
            interface_prompt = (
                " Use only the explicit capabilities: `inspect` for bounded "
                "read-only discovery, `apply_patch` for an exact old_text to "
                "new_text replacement (not a unified diff), "
                "`run_tests` for reproductions and verification, and `submit` "
                "to finish. DISCOVER permits at most "
                f"{discovery_inspection_limit} inspections. A successful "
                "apply_patch advances to VERIFY only when Git confirms a change. "
                "A passing run_tests result for the latest change advances to "
                "SUBMIT; a failure returns to IMPLEMENT. The submit capability "
                "generates the final patch itself, so do not create a submission "
                "file or use legacy submission commands."
            )
        else:
            interface_prompt = (
                " DISCOVER permits at most "
                f"{discovery_inspection_limit} read-only commands. IMPLEMENT "
                "requires a Git-confirmed working-tree change. VERIFY requires a "
                "focused reproduction or test, and SUBMIT follows the loaded "
                "submission skill. When complete, call `send_message`."
            )
        self.system_prompt = (
            f"{common_prompt}{interface_prompt}\n\n"
            f"<system_information>\n{system_information}\n</system_information>"
        )
        self.task_prompt = task

        if self.exposed_skills:
            catalog = "\n".join(
                skill["metadata"] for skill in self.exposed_skills.values()
            )
            self.system_prompt += (
                "\n\nReusable skills are available. Call `invoke_skill` with a "
                "skill's name to load its instructions, and follow them in place "
                f"of your default approach.\n\n<skills>\n{catalog}\n</skills>\n"
            )

    @staticmethod
    def _command_text(command: str | list[str]) -> str:
        """Return a normalized command string without interpreting the shell."""

        if isinstance(command, str):
            return command.strip()
        return " ".join(command).strip()

    @classmethod
    def _contains_forbidden_git_action(cls, command: str | list[str]) -> bool:
        """Reject model-issued Git operations that can hide or destroy a patch."""

        return cls._FORBIDDEN_GIT_PATTERN.search(cls._command_text(command)) is not None

    @classmethod
    def _classify_command(cls, command: str | list[str]) -> ExecuteKind:
        """Route confidently read-only commands and common phase actions.

        This routing never establishes that an edit happened; the internal
        ``git status --porcelain`` result provides that objective evidence.
        Unknown commands are routed to implementation so project-specific edit
        helpers are not incorrectly blocked.
        """

        text = cls._command_text(command)
        lowered = text.lower()
        # Shell-style environment assignments do not change the intent of the
        # command that follows them.
        lowered = re.sub(r"^(?:[a-z_]\w*=\S+\s+)+", "", lowered)
        if "patch.txt" in lowered or re.match(
            r"^git\s+format-patch(?:\s|$)", lowered
        ):
            return ExecuteKind.SUBMIT
        if any(pattern.search(lowered) for pattern in cls._VERIFY_PATTERNS):
            return ExecuteKind.VERIFY
        if re.match(r"^python(?:\d+(?:\.\d+)?)?\s+-\s*<<", lowered):
            writes_files = re.search(
                r"(?:write_text|write_bytes|\.write\s*\(|"
                r"open\s*\([^\n]*(?:['\"](?:w|a|x)[+b]?['\"])|"
                r"shutil\.(?:copy|move)|os\.(?:rename|replace|remove))",
                lowered,
            )
            return ExecuteKind.IMPLEMENT if writes_files else ExecuteKind.INSPECT
        if "<<" in text or re.search(r"(?:^|\s)(?:>>?|<>)\s*[^&]", text):
            return ExecuteKind.IMPLEMENT
        if re.match(r"^(?:sed|perl)(?:\s|$)", lowered) and re.search(
            r"(?:^|\s)-\w*i\w*(?:\s|$)", lowered
        ):
            return ExecuteKind.IMPLEMENT
        if cls._MUTATION_PATTERN.match(lowered):
            return ExecuteKind.IMPLEMENT
        if cls._INSPECTION_PATTERN.match(lowered):
            return ExecuteKind.INSPECT
        return ExecuteKind.IMPLEMENT

    def _transition(
        self,
        phase: CodeAgentPhase,
        *,
        inspections_used: int = 0,
    ) -> None:
        """Enter a phase and reset its local inspection counter."""

        self.phase = phase
        self.phase_inspections = inspections_used

    def _next_action(self) -> str:
        remaining = max(0, self.step_limit - self.steps_taken)
        if remaining <= 1:
            return "Use the required completion or submission tool now."
        if remaining <= 4 and self.phase == CodeAgentPhase.SUBMIT:
            return (
                "Complete the remaining submission steps now; do not inspect or edit."
            )
        if self.phase == CodeAgentPhase.DISCOVER:
            return "Call inspect only as needed to identify the root cause and edit site."
        if self.phase == CodeAgentPhase.IMPLEMENT:
            if self.tool_interface == "capability":
                return "Call apply_patch with the smallest source-code change."
            return "Make the smallest source-code change that addresses the root cause."
        if self.phase == CodeAgentPhase.VERIFY:
            if self.tool_interface == "capability":
                return "Call run_tests with a focused test for the changed behavior."
            return "Run a focused reproduction or test for the changed behavior."
        if self.tool_interface == "capability":
            return "Call submit with a concise, evidence-based summary."
        if "submit-task" in self.skills and "submit-task" not in self.invoked_skills:
            return "Invoke the submit-task skill."
        if not self.patch_created:
            return "Create patch.txt from only the source files changed for the fix."
        if not self.patch_reviewed:
            return "Read and verify patch.txt in a separate execute call."
        return "Call send_message with a concise, evidence-based summary."

    def _agent_state(self) -> str:
        """Expose compact deterministic state to the next model turn."""

        initial_revision = self.initial_revision or "unknown"
        interface_state = (
            f'applied_paths="{len(self.applied_paths)}"'
            if self.tool_interface == "capability"
            else (
                f'patch_created="{str(self.patch_created).lower()}" '
                f'patch_reviewed="{str(self.patch_reviewed).lower()}"'
            )
        )
        return (
            f'<agent_state phase="{self.phase.value}" '
            f'steps_remaining="{max(0, self.step_limit - self.steps_taken)}" '
            f'inspections="{self.total_inspections}" '
            f'phase_inspections="{self.phase_inspections}" '
            f'implementation_attempts="{self.implementation_attempts}" '
            f'verification_attempts="{self.verification_attempts}" '
            f'verification_status="{self.verification_status}" '
            f'change_revision="{self.change_revision}" '
            f'verified_revision="{self.verified_revision}" '
            f'initial_revision="{initial_revision}" '
            f'worktree_diff="{self.worktree_diff_status}" '
            f"{interface_state}>"
            f"<next_action>{self._next_action()}</next_action>"
            "</agent_state>"
        )

    @staticmethod
    def _working_tree_changed(
        status_result: dict[str, Any],
        diff_result: dict[str, Any] | None = None,
    ) -> bool:
        """Return whether git objectively reports a non-submission change."""

        if diff_result is not None and diff_result.get("returncode") == 0:
            diff_output = diff_result.get("output", "")
            if isinstance(diff_output, str) and diff_output.strip():
                return True

        if status_result.get("returncode") != 0:
            return False
        output = status_result.get("output", "")
        if not isinstance(output, str):
            return False
        for line in output.splitlines():
            # patch.txt is a submission artifact, not implementation progress.
            path = line[3:].strip() if len(line) >= 4 else ""
            if path and path != "patch.txt":
                return True
        return False

    def _ensure_initial_revision(self) -> None:
        """Capture the immutable comparison point before the first shell action."""

        if self.initial_revision_checked:
            return
        self.initial_revision_checked = True
        result = self.env.execute("git rev-parse HEAD")
        output = result.get("output", "")
        if result.get("returncode") == 0 and isinstance(output, str):
            revision = output.strip().splitlines()[0] if output.strip() else ""
            if re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
                self.initial_revision = revision

    def _baseline_diff(self) -> dict[str, Any]:
        """Diff all tracked work against the revision where this run started."""

        revision = self.initial_revision or "HEAD"
        return self.env.execute(
            f"git diff --binary --no-ext-diff {revision} -- ."
        )

    def _format_baseline_diff(
        self,
        diff_result: dict[str, Any],
        status_result: dict[str, Any],
    ) -> str:
        """Expose bounded proof of worktree progress without repeating the patch."""

        output = diff_result.get("output", "")
        valid_output = isinstance(output, str)
        changed = self._working_tree_changed(status_result, diff_result)
        digest = (
            hashlib.sha256(output.encode("utf-8")).hexdigest()
            if changed and valid_output and output.strip()
            else None
        )
        self.worktree_diff_digest = digest
        self.worktree_diff_status = (
            "changed"
            if changed
            else "clean"
            if diff_result.get("returncode") == 0
            else "unavailable"
        )
        revision = self.initial_revision or "unknown"
        byte_count = len(output.encode("utf-8")) if valid_output else 0
        formatted = (
            f'<git_diff_against_initial_revision revision="{revision}" '
            f'returncode="{diff_result.get("returncode", -1)}" '
            f'changed="{str(changed).lower()}" bytes="{byte_count}" '
            f'sha256="{digest or ""}"'
        )
        exception_info = diff_result.get("exception_info")
        if exception_info:
            return f"{formatted}><exception_info>{exception_info}</exception_info></git_diff_against_initial_revision>"
        return f"{formatted} />"

    @staticmethod
    def _format_git_status(status_result: dict[str, Any]) -> str:
        """Make the internal progress check explicit in the model observation."""

        formatted = (
            "<git_status_porcelain>\n"
            f"<returncode>{status_result.get('returncode', -1)}</returncode>\n"
            f"<output>{status_result.get('output', '')}</output>"
        )
        exception_info = status_result.get("exception_info")
        if exception_info:
            formatted += f"\n<exception_info>{exception_info}</exception_info>"
        return f"{formatted}\n</git_status_porcelain>"

    @staticmethod
    def _tool_error(message: str, *, multiline: bool = False) -> str:
        if multiline:
            return f"<tool_error>\n{message}\n</tool_error>"
        return f"<tool_error>{message}</tool_error>"

    @staticmethod
    def _safe_repo_path(path: Any, *, default: str = ".") -> str:
        """Validate and normalize a repository-relative POSIX path."""

        if path is None or (isinstance(path, str) and not path.strip()):
            return default
        if not isinstance(path, str) or "\x00" in path:
            raise ValueError("path must be a non-empty repository-relative string")
        candidate = PurePosixPath(path.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("path must stay within the repository")
        normalized = str(candidate)
        if normalized.startswith("-"):
            raise ValueError("path cannot start with '-'")
        return normalized

    def _status_observation(
        self,
        result: dict[str, Any],
        status_result: dict[str, Any],
    ) -> str:
        return (
            f"{format_tool_output(result)}\n"
            f"{self._format_git_status(status_result)}"
        )

    def _cached_diff(self, paths: set[str]) -> dict[str, Any]:
        return self.env.execute(
            [
                "git",
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
                *sorted(paths),
            ],
            shell=False,
        )

    @staticmethod
    def _result_digest(result: dict[str, Any]) -> str | None:
        output = result.get("output")
        if result.get("returncode") != 0 or not isinstance(output, str):
            return None
        return hashlib.sha256(output.encode("utf-8")).hexdigest()

    def _handle_inspect(self, arguments: dict[str, Any]) -> str:
        """Run one structured read-only repository inspection."""

        operation = arguments.get("operation")
        operations = {"list_files", "read_file", "search", "git_diff"}
        if operation not in operations:
            return self._tool_error(
                "inspect operation must be list_files, read_file, search, or git_diff"
            )
        # These properties share one public schema. Tolerate properties that are
        # irrelevant to the selected operation instead of charging a model turn
        # for a harmless schema/dispatcher mismatch.
        extra = set(arguments) - {
            "operation",
            "path",
            "query",
            "start_line",
            "end_line",
        }
        if extra:
            return self._tool_error(
                "Unknown inspect argument(s): " + ", ".join(sorted(extra)) + "."
            )
        try:
            path = self._safe_repo_path(arguments.get("path"))
        except ValueError as exc:
            return self._tool_error(str(exc))

        if operation == "list_files":
            argv = ["find", path, "-type", "f", "-not", "-path", "*/.git/*"]
        elif operation == "read_file":
            if path == ".":
                return self._tool_error("read_file requires a file path")
            start = arguments.get("start_line", 1)
            end = arguments.get("end_line", start + 199 if isinstance(start, int) else 200)
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start < 1
                or end < start
                or end - start >= 400
            ):
                return self._tool_error(
                    "read_file requires a valid range of at most 400 lines"
                )
            program = (
                f'NR >= {start} && NR <= {end} '
                '{ printf "%6d\\t%s\\n", NR, $0 }'
            )
            argv = ["awk", program, path]
        elif operation == "search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                return self._tool_error("search requires a non-empty string query")
            argv = [
                "grep",
                "-R",
                "-n",
                "-F",
                "--exclude-dir=.git",
                "--",
                query,
                path,
            ]
        else:
            argv = ["git", "diff", "--cached", "--no-ext-diff", "--", path]

        gate_error = self._admit_command(ExecuteKind.INSPECT)
        if gate_error:
            return self._tool_error(
                gate_error,
                multiline=gate_error == self.INSPECTION_BUDGET_ERROR,
            )
        result = self.env.execute(argv, shell=False)
        status_result = self.env.execute("git status --porcelain")
        return self._status_observation(result, status_result)

    def _handle_apply_patch(self, arguments: dict[str, Any]) -> str:
        """Apply one exact text replacement and stage the resulting file."""

        signature = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        call_count = self.edit_call_counts.get(signature, 0) + 1
        self.edit_call_counts[signature] = call_count

        def reject(message: str) -> str:
            self.implementation_attempts += 1
            suffix = ""
            if call_count > 1:
                suffix += " Do not repeat this identical failed edit."
            if self.implementation_attempts >= self.MAX_EDIT_FAILURES:
                suffix += (
                    " Edit failure budget reached; use the exact-replacement "
                    "contract shown in the tool schema."
                )
            return self._tool_error(message + suffix)

        required = {"path", "old_text", "new_text"}
        if set(arguments) != required:
            return reject(
                "apply_patch requires exactly path, old_text, and new_text strings."
            )
        if not all(isinstance(arguments.get(key), str) for key in required):
            return reject("apply_patch path, old_text, and new_text must be strings.")
        try:
            path = self._safe_repo_path(arguments["path"], default="")
        except ValueError as exc:
            return reject(str(exc))
        if not path or path == ".":
            return reject("apply_patch requires a file path.")
        if path == ".git" or path.startswith(".git/") or path == "patch.txt":
            return reject(f"apply_patch cannot modify protected path: {path}")

        old_text = arguments["old_text"]
        new_text = arguments["new_text"]
        if old_text == new_text:
            return reject("old_text and new_text must differ.")
        if not old_text and not new_text:
            return reject("new_text cannot be empty when creating a file.")

        old_payload = base64.b64encode(old_text.encode("utf-8")).decode("ascii")
        new_payload = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
        script = "\n".join(
            [
                "import base64, os, pathlib, subprocess, sys",
                "path = pathlib.Path(os.environ['ASSIGNMENT_EDIT_PATH'])",
                "old = base64.b64decode(os.environ['ASSIGNMENT_EDIT_OLD_B64']).decode('utf-8')",
                "new = base64.b64decode(os.environ['ASSIGNMENT_EDIT_NEW_B64']).decode('utf-8')",
                "if path.exists():",
                "    if not path.is_file():",
                "        print('target is not a regular file')",
                "        sys.exit(2)",
                "    if not old:",
                "        print('old_text must be non-empty for an existing file')",
                "        sys.exit(2)",
                "    data = path.read_text(encoding='utf-8')",
                "    count = data.count(old)",
                "    if count != 1:",
                "        print(f'old_text must occur exactly once; found {count}')",
                "        sys.exit(2)",
                "    updated = data.replace(old, new, 1)",
                "else:",
                "    if old:",
                "        print('target does not exist; old_text must be empty to create it')",
                "        sys.exit(2)",
                "    if not path.parent.is_dir():",
                "        print('parent directory does not exist')",
                "        sys.exit(2)",
                "    updated = new",
                "path.write_text(updated, encoding='utf-8')",
                "result = subprocess.run(['git', 'add', '--', str(path)], "
                "stdout=subprocess.PIPE, stderr=subprocess.STDOUT)",
                "sys.stdout.buffer.write(result.stdout)",
                "sys.exit(result.returncode)",
            ]
        )
        result = self.env.execute(
            ["python", "-c", script],
            shell=False,
            env={
                "ASSIGNMENT_EDIT_PATH": path,
                "ASSIGNMENT_EDIT_OLD_B64": old_payload,
                "ASSIGNMENT_EDIT_NEW_B64": new_payload,
            },
        )
        status_result = self.env.execute("git status --porcelain")
        self.implementation_attempts += 1
        self.verified_revision = None
        self.verified_digest = None
        self.patch_created = False
        self.patch_reviewed = False

        if result.get("returncode") == 0 and self._working_tree_changed(status_result):
            self.applied_paths.add(path)
            diff_result = self._cached_diff(self.applied_paths)
            digest = self._result_digest(diff_result)
            if digest and diff_result.get("output", "").strip():
                self.change_revision += 1
                self.change_digest = digest
                self.verification_status = "required"
                self.has_successful_implementation = True
                self._transition(CodeAgentPhase.VERIFY)
            else:
                self.change_digest = None
                self.verification_status = "change_not_detected"
                self._transition(CodeAgentPhase.IMPLEMENT)
        else:
            self.verification_status = "change_not_detected"
            self._transition(CodeAgentPhase.IMPLEMENT)
        return self._status_observation(result, status_result)

    def _handle_run_tests(self, arguments: dict[str, Any]) -> str:
        """Run a focused command and bind its result to the current revision."""

        allowed = {"argv", "cwd", "env", "timeout"}
        extra = set(arguments) - allowed
        argv = arguments.get("argv")
        env = arguments.get("env")
        timeout = arguments.get("timeout")
        if extra:
            return self._tool_error(
                "Unknown run_tests argument(s): " + ", ".join(sorted(extra)) + "."
            )
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) and part for part in argv
        ):
            return self._tool_error("run_tests requires a non-empty argv string list")
        if env is not None and (
            not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            return self._tool_error("run_tests env must map strings to strings")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            return self._tool_error("run_tests timeout must be a positive number")
        try:
            relative_cwd = self._safe_repo_path(arguments.get("cwd"))
        except ValueError as exc:
            return self._tool_error(str(exc))
        cwd = None if relative_cwd == "." else posixpath.join(self.env.cwd, relative_cwd)

        before_status = self.env.execute("git status --porcelain")
        result = self.env.execute(
            argv,
            shell=False,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
        status_result = self.env.execute("git status --porcelain")
        self.verification_attempts += 1
        status_stable = (
            before_status.get("returncode") == status_result.get("returncode") == 0
            and before_status.get("output") == status_result.get("output")
        )

        if self.change_revision:
            diff_result = self._cached_diff(self.applied_paths)
            digest = self._result_digest(diff_result)
            if result.get("returncode") == 0 and status_stable and digest == self.change_digest:
                self.verified_revision = self.change_revision
                self.verified_digest = digest
                self.verification_status = "passed"
                self._transition(CodeAgentPhase.SUBMIT)
            else:
                self.verified_revision = None
                self.verified_digest = None
                self.verification_status = (
                    "failed"
                    if result.get("returncode") != 0
                    else "working_tree_changed_during_verification"
                )
                self._transition(CodeAgentPhase.IMPLEMENT)
        else:
            self.verification_status = (
                "baseline_passed" if result.get("returncode") == 0 else "baseline_failed"
            )
        return self._status_observation(result, status_result)

    def _handle_submit(self, arguments: dict[str, Any]) -> str:
        """Validate the verified revision and capture its patch for the CLI."""

        if set(arguments) != {"summary"} or not isinstance(
            arguments.get("summary"), str
        ):
            return self._tool_error("submit requires exactly one string summary")
        if self.phase != CodeAgentPhase.SUBMIT:
            return self._tool_error(
                "Submission is gated until apply_patch is followed by passing run_tests."
            )
        if self.verified_revision != self.change_revision or not self.verified_digest:
            return self._tool_error("The latest code revision has not passed verification.")

        status_result = self.env.execute("git status --porcelain")
        status_output = status_result.get("output", "")
        if status_result.get("returncode") != 0 or not isinstance(status_output, str):
            return self._tool_error("Could not validate repository status before submission.")
        unsafe_lines = [
            line
            for line in status_output.splitlines()
            if line.startswith("??") or (len(line) > 1 and line[1] != " ")
        ]
        if unsafe_lines:
            self._transition(CodeAgentPhase.IMPLEMENT)
            self.verification_status = "unverified_worktree_changes"
            return self._tool_error(
                "Unverified working-tree changes appeared after apply_patch: "
                + "; ".join(unsafe_lines)
            )

        diff_result = self._cached_diff(self.applied_paths)
        digest = self._result_digest(diff_result)
        patch = diff_result.get("output", "")
        if (
            not isinstance(patch, str)
            or not patch.strip()
            or digest != self.verified_digest
        ):
            self._transition(CodeAgentPhase.IMPLEMENT)
            self.verification_status = "revision_changed_after_verification"
            return self._tool_error(
                "The submitted diff is empty or differs from the verified revision."
            )

        self.submitted_patch = patch
        self.finished = True
        paths = ", ".join(sorted(self.applied_paths))
        return (
            f"<submission accepted=\"true\" bytes=\"{len(patch.encode('utf-8'))}\">"
            f"<paths>{paths}</paths><summary>{arguments['summary']}</summary>"
            "</submission>\n"
            f"{self._format_git_status(status_result)}"
        )

    def _admit_command(self, kind: ExecuteKind) -> str | None:
        """Advance inspection counters or explain why a command is gated."""

        remaining = max(0, self.step_limit - self.steps_taken)
        if self.step_limit >= 10 and kind == ExecuteKind.INSPECT and remaining <= 6:
            return (
                "Inspection is closed with six or fewer model calls remaining. "
                "Implement, verify, and submit now."
            )

        if kind == ExecuteKind.SUBMIT and self.phase != CodeAgentPhase.SUBMIT:
            return (
                "Submission actions are gated until a source change has been "
                "successfully verified."
            )
        if (
            kind == ExecuteKind.SUBMIT
            and "submit-task" in self.skills
            and "submit-task" not in self.invoked_skills
        ):
            return "Invoke the submit-task skill before creating or reading patch.txt."

        if kind != ExecuteKind.INSPECT:
            return None

        if self.phase == CodeAgentPhase.SUBMIT:
            return (
                "General inspection is closed in SUBMIT. Invoke the submission "
                "skill, create patch.txt, review it, and send the summary."
            )

        limit = (
            self.discovery_inspection_limit
            if self.phase == CodeAgentPhase.DISCOVER
            else self.phase_inspection_limit
        )
        if self.phase_inspections >= limit:
            if self.phase == CodeAgentPhase.DISCOVER:
                # Saturate the next phase's allowance so additional browsing is
                # blocked until an implementation attempt creates new evidence.
                self._transition(
                    CodeAgentPhase.IMPLEMENT,
                    inspections_used=self.phase_inspection_limit,
                )
            return self.INSPECTION_BUDGET_ERROR

        self.phase_inspections += 1
        self.total_inspections += 1
        if (
            self.phase == CodeAgentPhase.DISCOVER
            and self.phase_inspections == self.discovery_inspection_limit
        ):
            self._transition(
                CodeAgentPhase.IMPLEMENT,
                inspections_used=self.phase_inspection_limit,
            )
        return None

    def _record_command_result(
        self,
        kind: ExecuteKind,
        command: str | list[str],
        returncode: int,
        status_result: dict[str, Any],
        diff_result: dict[str, Any],
    ) -> None:
        """Apply state transitions after an admitted command completes."""

        if kind == ExecuteKind.INSPECT:
            return
        if kind == ExecuteKind.IMPLEMENT:
            self.implementation_attempts += 1
            self.patch_created = False
            self.patch_reviewed = False
            working_tree_changed = self._working_tree_changed(status_result, diff_result)
            self.verification_status = (
                "required" if working_tree_changed else "change_not_detected"
            )
            if returncode == 0 and working_tree_changed:
                self.has_successful_implementation = True
                self._transition(CodeAgentPhase.VERIFY)
            else:
                self._transition(CodeAgentPhase.IMPLEMENT)
            return
        if kind == ExecuteKind.VERIFY:
            self.verification_attempts += 1
            self.verification_status = "passed" if returncode == 0 else "failed"
            if (
                self.has_successful_implementation
                and self._working_tree_changed(status_result, diff_result)
            ):
                self._transition(
                    CodeAgentPhase.SUBMIT
                    if returncode == 0
                    else CodeAgentPhase.IMPLEMENT
                )
            elif self.has_successful_implementation:
                self.verification_status = "change_not_detected"
                self._transition(CodeAgentPhase.IMPLEMENT)
            return

        text = self._command_text(command).lower()
        creates_patch = bool(
            re.search(r">\s*(?:\S*/)?patch\.txt(?:\s|$)", text)
        )
        reviews_patch = bool(
            re.match(r"^(?:cat|sed|head|tail|nl|less|more)(?:\s|$)", text)
        )
        if creates_patch and returncode == 0:
            self.patch_created = True
            self.patch_reviewed = False
        elif self.patch_created and reviews_patch and returncode == 0:
            self.patch_reviewed = True

    def execute_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Dispatch capability tools or the optional legacy shell interface."""

        observations: list[dict[str, str]] = []

        def add_observation(call_id: str, content: str) -> None:
            observations.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"{content}\n{self._agent_state()}",
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

            if self.tool_interface == "capability":
                capability_handlers = {
                    "inspect": self._handle_inspect,
                    "apply_patch": self._handle_apply_patch,
                    "run_tests": self._handle_run_tests,
                    "submit": self._handle_submit,
                }
                handler = capability_handlers.get(name)
                if handler is not None:
                    add_observation(call_id, handler(arguments))
                    continue

            if name == "execute" and self.tool_interface == "legacy":
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
                elif self._contains_forbidden_git_action(command):
                    add_observation(
                        call_id,
                        tool_error(
                            "Git staging/history mutation is forbidden. Do not run "
                            "git add, commit, reset, restore, checkout, switch, "
                            "stash, clean, rm, mv, merge, rebase, or cherry-pick. "
                            "Keep source changes uncommitted and continue with "
                            "verification or submission."
                        ),
                    )
                else:
                    kind = self._classify_command(command)
                    gate_error = self._admit_command(kind)
                    if gate_error is not None:
                        error_content = (
                            f"<tool_error>\n{gate_error}\n</tool_error>"
                            if gate_error == self.INSPECTION_BUDGET_ERROR
                            else tool_error(f"Phase gate: {gate_error}")
                        )
                        add_observation(
                            call_id,
                            error_content,
                        )
                        continue
                    call_signature = json.dumps(
                        arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    call_count = self.execute_call_counts.get(call_signature, 0) + 1
                    self.execute_call_counts[call_signature] = call_count
                    self._ensure_initial_revision()
                    result = self.env.execute(
                        command,
                        shell=shell,
                        cwd=cwd,
                        timeout=timeout,
                        env=env,
                    )
                    diff_result = self._baseline_diff()
                    status_result = self.env.execute("git status --porcelain")
                    self._record_command_result(
                        kind,
                        command,
                        int(result["returncode"]),
                        status_result,
                        diff_result,
                    )
                    observation = (
                        f"{format_tool_output(result)}\n"
                        f"{self._format_git_status(status_result)}\n"
                        f"{self._format_baseline_diff(diff_result, status_result)}"
                    )
                    if call_count > 1:
                        observation += (
                            "\n<progress_warning>This exact execute call has now "
                            f"run {call_count} times. If its prior result already "
                            "answered the question, take a materially different "
                            "action instead of repeating inspection."
                            "</progress_warning>"
                        )
                    add_observation(call_id, observation)

            elif name == "send_message" and self.tool_interface == "legacy":
                if set(arguments) != {"summary"} or not isinstance(
                    arguments.get("summary"), str
                ):
                    add_observation(
                        call_id,
                        tool_error("send_message requires exactly one string summary."),
                    )
                    continue

                requires_submission_skill = "submit-task" in self.skills
                if requires_submission_skill and self.phase != CodeAgentPhase.SUBMIT:
                    add_observation(
                        call_id,
                        tool_error(
                            "Submission is phase-gated until a source change has "
                            "passed focused verification."
                        ),
                    )
                    continue

                # A trajectory is saved even when a run fails, but downstream
                # evaluation needs the separate patch artifact. Prevent a model
                # from declaring success before completing the submission flow.
                if requires_submission_skill:
                    if "submit-task" not in self.invoked_skills:
                        add_observation(
                            call_id,
                            tool_error(
                                "Submission is incomplete. Invoke the "
                                "submit-task skill before sending the summary."
                            ),
                        )
                        continue
                if requires_submission_skill and not self.patch_reviewed:
                    add_observation(
                        call_id,
                        tool_error(
                            "Submission is incomplete. Create, then read and "
                            "verify patch.txt in a separate execute call."
                        ),
                    )
                    continue
                if requires_submission_skill:
                    patch_check = self.env.execute("test -s patch.txt")
                    if patch_check["returncode"] != 0:
                        self.patch_created = False
                        self.patch_reviewed = False
                        add_observation(
                            call_id,
                            tool_error(
                                "Submission is incomplete. Create a non-empty "
                                "patch.txt before submitting."
                            ),
                        )
                        continue
                    patch_result = self.env.execute("cat patch.txt")
                    if patch_result["returncode"] != 0:
                        add_observation(
                            call_id,
                            tool_error(
                                "Submission is incomplete. Could not read patch.txt."
                            ),
                        )
                        continue
                    self.submitted_patch = patch_result["output"]

                summary = arguments["summary"]
                self.finished = True
                add_observation(call_id, summary)

            elif name == "invoke_skill" and self.exposed_skills:
                if set(arguments) != {"name"} or not isinstance(
                    arguments.get("name"), str
                ):
                    add_observation(
                        call_id,
                        tool_error("invoke_skill requires exactly one string name."),
                    )
                elif arguments["name"] not in self.exposed_skills:
                    add_observation(
                        call_id,
                        tool_error(f"Unknown skill: {arguments['name']}."),
                    )
                elif (
                    arguments["name"] == "submit-task"
                    and self.phase != CodeAgentPhase.SUBMIT
                ):
                    add_observation(
                        call_id,
                        tool_error(
                            "The submit-task skill is phase-gated until a source "
                            "change has passed focused verification."
                        ),
                    )
                else:
                    self.invoked_skills.add(arguments["name"])
                    add_observation(
                        call_id,
                        self.exposed_skills[arguments["name"]]["content"],
                    )

            else:
                add_observation(call_id, tool_error(f"Unknown tool: {name}."))

        return observations
