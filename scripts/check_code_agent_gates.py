"""Offline smoke checks for CodeAgent phase gates and capability tools.

Run with ``uv run python scripts/check_code_agent_gates.py``. No model request,
Modal sandbox, Docker container, or API credit is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from assignment.agent import CodeAgent
from assignment.agent.code_agent import CodeAgentPhase


PATCH = """diff --git a/django/db/models/query.py b/django/db/models/query.py
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1 +1 @@
-old
+new
"""


class FakeEnvironment:
    cwd = "/testbed"
    system = "Linux"
    release = "6.1"
    version = "#1"
    machine = "x86_64"

    def __init__(self) -> None:
        self.commands: list[object] = []
        self.dirty = False

    def execute(self, command, **kwargs):
        self.commands.append(command)
        edit_env = kwargs.get("env") or {}
        if "ASSIGNMENT_EDIT_PATH" in edit_env or command == "python -c 'edit'":
            self.dirty = True
        if command == "cat patch.txt":
            return {"output": PATCH, "returncode": 0}
        if command == "git status --porcelain":
            return {
                "output": "M  django/db/models/query.py\n" if self.dirty else "",
                "returncode": 0,
            }
        if isinstance(command, list) and command[:3] == ["git", "diff", "--cached"]:
            return {"output": PATCH if self.dirty else "", "returncode": 0}
        return {"output": "", "returncode": 0}


class LocalGitEnvironment:
    system = "Linux"
    release = "6.1"
    version = "#1"
    machine = "x86_64"

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def execute(self, command, **kwargs):
        if isinstance(command, list) and command and command[0] == "python":
            command = [sys.executable, *command[1:]]
        shell = kwargs.get("shell")
        if shell is None:
            shell = isinstance(command, str)
        environment = {**os.environ, **(kwargs.get("env") or {})}
        result = subprocess.run(
            command,
            cwd=kwargs.get("cwd") or self.cwd,
            env=environment,
            shell=shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=kwargs.get("timeout"),
            check=False,
        )
        return {"output": result.stdout, "returncode": result.returncode}


def call(agent: CodeAgent, call_id: str, name: str, arguments: dict) -> str:
    observations = agent.execute_tool_calls(
        [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ]
    )
    return observations[0]["content"]


def main() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "offline-smoke-test")
    os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:1/v1")

    default_agent = CodeAgent("task", FakeEnvironment(), model="gpt-5-mini")
    default_tools = {tool["function"]["name"] for tool in default_agent.tools}
    assert {"execute", "send_message"} <= default_tools
    assert "apply_patch" not in default_tools

    legacy_environment = FakeEnvironment()
    legacy_agent = CodeAgent(
        "task",
        legacy_environment,
        model="gpt-5-mini",
        skills_path="tasks/code-skills",
    )
    call(
        legacy_agent,
        "legacy-edit",
        "execute",
        {"command": "python -c 'edit'"},
    )
    assert legacy_agent.phase == CodeAgentPhase.VERIFY
    call(
        legacy_agent,
        "legacy-test",
        "execute",
        {"command": "pytest -q tests/queries/test_bulk_update.py"},
    )
    assert legacy_agent.phase == CodeAgentPhase.SUBMIT
    call(legacy_agent, "legacy-skill", "invoke_skill", {"name": "submit-task"})
    call(
        legacy_agent,
        "legacy-patch",
        "execute",
        {"command": "git diff -- django/db/models/query.py > patch.txt"},
    )
    call(
        legacy_agent,
        "legacy-review",
        "execute",
        {"command": "cat patch.txt"},
    )
    call(
        legacy_agent,
        "legacy-submit",
        "send_message",
        {"summary": "Applied and verified the focused fix."},
    )
    assert legacy_agent.finished
    assert legacy_agent.submitted_patch == PATCH

    environment = FakeEnvironment()
    agent = CodeAgent(
        "task",
        environment,
        model="gpt-5-mini",
        tool_interface="capability",
    )

    for index in range(6):
        observation = call(
            agent,
            f"inspect-{index}",
            "inspect",
            {"operation": "list_files", "path": "."},
        )
        assert "<tool_error>" not in observation
    assert agent.phase == CodeAgentPhase.IMPLEMENT

    commands_before = list(environment.commands)
    rejected = call(
        agent,
        "inspect-7",
        "inspect",
        {"operation": "read_file", "path": "django/db/models/query.py"},
    )
    assert "Inspection budget exhausted" in rejected
    assert environment.commands == commands_before

    edited = call(
        agent,
        "edit",
        "apply_patch",
        {
            "path": "django/db/models/query.py",
            "old_text": "if not isinstance(attr, Expression):",
            "new_text": "if not hasattr(attr, 'resolve_expression'):",
        },
    )
    assert agent.phase == CodeAgentPhase.VERIFY
    assert agent.phase_inspections == 0
    assert "M  django/db/models/query.py" in edited

    verified = call(
        agent,
        "verify",
        "run_tests",
        {"argv": ["python", "tests/runtests.py", "queries.test_bulk_update"]},
    )
    assert agent.phase == CodeAgentPhase.SUBMIT
    assert agent.verification_status == "passed"
    assert 'phase="submit"' in verified

    submitted = call(
        agent,
        "submit",
        "submit",
        {"summary": "Applied and verified the focused fix."},
    )
    assert agent.finished
    assert agent.submitted_patch == PATCH
    assert 'accepted="true"' in submitted

    with tempfile.TemporaryDirectory(prefix="code-agent-smoke-") as directory:
        repository = Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "offline@example.invalid"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Offline Smoke"],
            cwd=repository,
            check=True,
        )
        source = repository / "example.py"
        source.write_text("value = 'old'\n", encoding="utf-8")
        subprocess.run(["git", "add", "example.py"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)

        integration_agent = CodeAgent(
            "task",
            LocalGitEnvironment(directory),
            model="gpt-5-mini",
            tool_interface="capability",
        )
        integration_edit = call(
            integration_agent,
            "integration-edit",
            "apply_patch",
            {
                "path": "example.py",
                "old_text": "value = 'old'",
                "new_text": "value = 'new'",
            },
        )
        assert "<tool_error>" not in integration_edit
        assert source.read_text(encoding="utf-8") == "value = 'new'\n"
        call(
            integration_agent,
            "integration-test",
            "run_tests",
            {
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; assert 'new' in Path('example.py').read_text()",
                ]
            },
        )
        call(
            integration_agent,
            "integration-submit",
            "submit",
            {"summary": "Verified exact replacement."},
        )
        assert integration_agent.finished
        assert "value = 'new'" in integration_agent.submitted_patch
    print("CodeAgent offline gate checks passed.")


if __name__ == "__main__":
    main()
