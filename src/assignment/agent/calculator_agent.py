"""A tiny ReAct agent for learning the harness without running a sandbox."""

from __future__ import annotations

import json
import math
import operator
from typing import Any, Callable

from assignment.agent.base import Agent
from assignment.agent.tools import CALCULATE_TOOL, SUBMIT_ANSWER_TOOL


class _NoopEnvironment:
    """Satisfy the shared Agent interface when a domain needs no environment.

    CodeAgent and ChessAgent act on isolated services. Arithmetic is pure local
    computation, so creating a container would only obscure this small example.
    """

    def stop(self) -> None:
        """Mirror the sandbox cleanup method; there is nothing to release."""


class CalculatorAgent(Agent):
    """Solve an arithmetic question through checked calculator tool calls."""

    OPERATIONS: dict[str, Callable[[int | float, int | float], int | float]] = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": operator.truediv,
    }

    def __init__(
        self,
        question: str,
        model: str | None = None,
        logs_save_path: str | None = None,
        step_limit: int = 8,
    ):
        if not question.strip():
            raise ValueError("Calculator question cannot be empty.")

        super().__init__(
            environment=_NoopEnvironment(),
            model=model,
            logs_save_path=logs_save_path,
            step_limit=step_limit,
        )
        self.tools.extend([CALCULATE_TOOL, SUBMIT_ANSWER_TOOL])
        # Small local models sometimes print a JSON-looking call as prose when
        # tool selection is automatic. Requiring a tool keeps this teaching
        # example on the same structured action/observation path every turn.
        self.tool_choice = "required"
        self.final_answer: str | None = None

        self.system_prompt = (
            "You are a careful calculator agent. Use `calculate` for every "
            "arithmetic operation, including simple ones, and chain tool results "
            "when the question needs multiple operations. Do not estimate or do "
            "arithmetic mentally. When you have the result, call `submit_answer` "
            "exactly once."
        )
        self.task_prompt = question.strip()

    @staticmethod
    def _error(message: str) -> str:
        """Return a recoverable observation instead of crashing the agent loop."""

        return f"<calculator_error>{message}</calculator_error>"

    def execute_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Validate and execute calculator-domain tools.

        As in the heavier agents, every call receives a linked tool observation.
        Malformed calls are reported to the model so it can correct itself on
        the next shared ReAct step.
        """

        observations: list[dict[str, str]] = []

        def observe(call_id: str, content: str) -> None:
            observations.append(
                {"role": "tool", "tool_call_id": call_id, "content": content}
            )

        for call in tool_calls:
            if not isinstance(call, dict):
                observe("unknown", self._error("Malformed tool call."))
                continue

            call_id = str(call.get("id", "unknown"))
            function = call.get("function")
            if not isinstance(function, dict):
                observe(call_id, self._error("Malformed tool call."))
                continue

            name = function.get("name")
            raw_arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                observe(
                    call_id,
                    self._error("Tool name and arguments must be strings."),
                )
                continue

            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                observe(call_id, self._error(f"Arguments are not valid JSON: {exc.msg}."))
                continue
            if not isinstance(arguments, dict):
                observe(call_id, self._error("Tool arguments must be a JSON object."))
                continue

            if name == "calculate":
                observe(call_id, self._calculate(arguments))
            elif name == "submit_answer":
                answer = arguments.get("answer")
                if set(arguments) != {"answer"} or isinstance(answer, bool) or not isinstance(
                    answer, (str, int, float)
                ):
                    observe(
                        call_id,
                        self._error("submit_answer requires exactly one answer."),
                    )
                    continue
                if isinstance(answer, (int, float)) and not math.isfinite(answer):
                    observe(call_id, self._error("The final answer must be finite."))
                    continue
                self.final_answer = str(answer).strip()
                if not self.final_answer:
                    observe(call_id, self._error("The final answer cannot be empty."))
                    self.final_answer = None
                    continue
                self.finished = True
                observe(call_id, f"<final_answer>{self.final_answer}</final_answer>")
            else:
                observe(call_id, self._error(f"Unknown tool: {name}."))

        return observations

    def _calculate(self, arguments: dict[str, Any]) -> str:
        """Perform one safe operation and format its result for the model."""

        if set(arguments) != {"operation", "left", "right"}:
            return self._error(
                "calculate requires exactly operation, left, and right."
            )

        operation = arguments["operation"]
        left = self._coerce_number(arguments["left"])
        right = self._coerce_number(arguments["right"])
        if not isinstance(operation, str) or operation not in self.OPERATIONS:
            return self._error(f"Unknown operation: {operation}.")
        if left is None or right is None:
            return self._error("left and right must be finite JSON numbers.")
        if operation == "divide" and right == 0:
            return self._error("Division by zero is undefined.")

        try:
            result = self.OPERATIONS[operation](left, right)
            finite_result = math.isfinite(result)
        except (ArithmeticError, OverflowError):
            finite_result = False
        if not finite_result:
            return self._error("The result is outside the supported numeric range.")
        return json.dumps(
            {
                "operation": operation,
                "left": left,
                "right": right,
                "result": result,
            }
        )

    @staticmethod
    def _coerce_number(value: Any) -> int | float | None:
        """Accept finite numbers and numeric strings produced by small LLMs."""

        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return value if math.isfinite(value) else None
            except OverflowError:
                return None
        if not isinstance(value, str):
            return None
        try:
            number = float(value.strip())
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
