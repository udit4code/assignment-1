"""Chess tool implementations, decoupled from the agent that registers them.

Every function here takes the HTTP client explicitly instead of reading it off
an agent, so the same code can run in the agent process or inside the sandbox
beside the server it talks to.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

CHESS_PORT = 8000


def _request_state(
    client: httpx.Client, method: str, endpoint: str, **kwargs: Any
) -> dict[str, Any]:
    """Make one chess API request and validate its JSON response."""

    response = client.request(method, endpoint, **kwargs)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Chess server returned non-JSON ({response.status_code})."
        ) from exc
    if response.status_code >= 400:
        detail = (
            payload.get("detail", payload) if isinstance(payload, dict) else payload
        )
        raise ValueError(str(detail))
    if not isinstance(payload, dict):
        raise RuntimeError("Chess server response must be a JSON object.")
    return payload


def _simulate_move(client: httpx.Client, arguments: str) -> str:
    """New tool: inspect FEN or simulate one ply without changing the game.

    Takes the raw JSON arguments of one tool call and returns the observation
    to send back, so a bad argument or a server error reaches the model as a
    recoverable ``<chess_error>`` instead of ending the run.
    """
    try:
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("simulate_move arguments must be a JSON object")

        extra = set(parsed) - {"fen", "move"}
        if extra:
            names = ", ".join(sorted(extra))
            raise ValueError(f"Unknown simulate_move argument(s): {names}")

        fen = parsed.get("fen")
        if not isinstance(fen, str) or not fen.strip():
            raise ValueError("fen must be a non-empty string")

        move = parsed.get("move")
        if move is not None and (
            not isinstance(move, str) or not move.strip()
        ):
            raise ValueError("move must be a non-empty UCI string or null")

        payload: dict[str, str | None] = {"fen": fen}
        if "move" in parsed:
            payload["move"] = move

        state = _request_state(
            client,
            "POST",
            "/api/simulate",
            json=payload,
        )
        return json.dumps(state)
    except Exception as exc:
        return f"<chess_error>{type(exc).__name__}: {exc}</chess_error>"


def _play_move(client: httpx.Client, arguments: str) -> str:
    """Existing tool: play one move as White and return the resulting state.

    Takes the raw JSON arguments of one tool call. Returns the new state, or a
    `<chess_error>` observation if the move could not be played.
    """
    try:
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("play_move arguments must be a JSON object")
        if set(parsed) != {"move"}:
            raise ValueError("play_move requires exactly one argument named move")

        move = parsed["move"]
        if not isinstance(move, str) or not move.strip():
            raise ValueError("move must be a non-empty string in UCI notation")

        state = _request_state(
            client,
            "POST",
            "/api/move",
            json={"move": move},
        )
        return json.dumps(state)
    except Exception as exc:
        return f"<chess_error>{type(exc).__name__}: {exc}</chess_error>"


def _run_python(env: Any, port: int, arguments: str) -> str:
    """New tool: run Python with access to the existing registered tools.

    The snippet runs inside the sandbox, which already has the tool
    implementations and the chess server, so code the model wrote never
    executes in the agent process.
    """
    def chess_error(message: str) -> str:
        return f"<chess_error>{message}</chess_error>"

    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        return chess_error(f"Invalid run_python JSON: {exc}")

    if not isinstance(parsed, dict):
        return chess_error("run_python arguments must be a JSON object")
    if set(parsed) != {"code"}:
        return chess_error("run_python requires exactly one argument named code")

    code = parsed["code"]
    if not isinstance(code, str) or not code.strip():
        return chess_error("code must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return chess_error("sandbox port must be an integer from 1 to 65535")

    encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
    result = env.execute(
        [
            "python",
            "/opt/assignment/sandbox_python.py",
            str(port),
            encoded_code,
        ],
        shell=False,
    )
    if not isinstance(result, dict):
        return chess_error("Sandbox returned an invalid command result")

    if result.get("returncode") != 0:
        detail = (
            result.get("exception_info")
            or result.get("stderr")
            or result.get("output")
            or f"sandbox runner exited with code {result.get('returncode')}"
        )
        return chess_error(str(detail).strip())

    output = result.get("output", result.get("stdout"))
    if not isinstance(output, str):
        return chess_error("Sandbox runner produced no text output")
    return output


def _invoke_skill(skills: dict[str, dict[str, str]], arguments: str) -> str:
    """Existing tool: load one skill's instructions into the conversation."""

    def chess_error(message: str) -> str:
        return f"<chess_error>{message}</chess_error>"

    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        return chess_error(f"Invalid invoke_skill JSON: {exc}")

    if not isinstance(parsed, dict):
        return chess_error("invoke_skill arguments must be a JSON object")
    if set(parsed) != {"name"}:
        return chess_error("invoke_skill requires exactly one argument named name")

    name = parsed["name"]
    if not isinstance(name, str) or not name.strip():
        return chess_error("skill name must be a non-empty string")
    if name not in skills:
        return chess_error(f"Unknown skill: {name}")

    skill = skills[name]
    content = skill.get("content") if isinstance(skill, dict) else None
    if not isinstance(content, str):
        return chess_error(f"Skill has no readable content: {name}")
    return content


def _game_state(client: httpx.Client, reset: bool = False) -> dict:
    """Read the live game, or start a new one and read the opening position."""

    method, endpoint = ("POST", "/api/reset") if reset else ("GET", "/api/state")
    return _request_state(client, method, endpoint)
