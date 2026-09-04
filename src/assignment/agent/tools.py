"""Tool definitions exposed to the model, in the OpenAI tool-calling format."""

INSPECT_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect",
        "description": (
            "Perform one read-only repository inspection. Use list_files to list "
            "files, read_file to read a numbered bounded line range, search to find text, "
            "or git_diff to inspect current changes. Inspection calls are quota "
            "limited by the harness."
        ),
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list_files", "read_file", "search", "git_diff"],
                },
                "path": {
                    "type": "string",
                    "description": "Repository-relative file or directory; defaults to .",
                },
                "query": {
                    "type": "string",
                    "description": "Literal text to find; required for search",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line for read_file; defaults to 1",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Last line for read_file; defaults to start_line + 199",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}

APPLY_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": (
            "Make one exact, repository-relative text edit. For an existing file, "
            "old_text must occur exactly once and is replaced by new_text. To create "
            "a file, use an empty old_text and a non-empty new_text. The harness "
            "stages the result, confirms the Git change, and requires verification."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Repository-relative file path",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace; empty only when creating a file",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text, or complete contents for a new file",
                },
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
}

RUN_TESTS_TOOL = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": (
            "Run one focused reproduction, test, type check, or build command. "
            "Pass an argv list; the command is not interpreted by a shell. A passing "
            "result advances to submission only after a confirmed code change."
        ),
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional repository-relative working directory",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "timeout": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
}

SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": (
            "Submit the verified solution. The harness confirms that the latest "
            "code revision passed, generates the git patch, and ends the run."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise summary of changes and verification",
                }
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}

CODE_CAPABILITY_TOOLS = [
    INSPECT_TOOL,
    APPLY_PATCH_TOOL,
    RUN_TESTS_TOOL,
    SUBMIT_TOOL,
]

EXECUTE_TOOL = {
    "type": "function",
    "function": {
        "name": "execute",
        "description": (
            "Run a bash command and return its stdout, stderr, and exit code. "
            "A non-zero exit code is reported, not raised.\n"
            "\n"
            "Every command runs in a new subshell, so a `cd` or an export does not "
            "carry over to the next command. Use the `cwd` and `env` arguments "
            "instead. Files you write do persist.\n"
            "\n"
            "Commands are non-interactive and cannot prompt for input, so pass "
            "flags like `-y` where a command would otherwise ask for confirmation. "
            "Prefer commands that produce little output; when reading a file, use "
            "`head`, `tail`, or `sed -n '10,20p'` rather than printing all of it.\n"
            "\n"
            "Useful patterns:\n"
            "- Create a file: `cat <<'EOF' > newfile.py` ... `EOF`\n"
            "- Edit in place: `sed -i 's/old/new/g' filename.py` (drop the trailing "
            "`g` to replace only the first match; restrict to a line range with "
            "`sed -i '1,10s/old/new/g'`)\n"
            "- View numbered lines: `nl -ba filename.py | sed -n '10,20p'`"
        ),
        # The nested env object intentionally accepts arbitrary variable names,
        # which is incompatible with strict schemas on some providers.
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "anyOf": [
                        {
                            "type": "string",
                            "description": 'A shell command line, e.g. "ls -la | head".',
                        },
                        {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                'The command as an argv list, e.g. ["ls", "-la"]. '
                                "Use this with shell=false when arguments contain "
                                "characters the shell would interpret."
                            ),
                        },
                    ],
                    "description": "The command to run.",
                },
                "shell": {
                    "type": ["boolean", "null"],
                    "description": (
                        "Whether to run the command through a shell, which enables "
                        "pipes, redirection, and globbing. Defaults to true. Set to "
                        "false when passing an argv list."
                    ),
                },
                "cwd": {
                    "type": ["string", "null"],
                    "description": (
                        "Absolute path to run the command in. Defaults to the "
                        "sandbox's current working directory."
                    ),
                },
                "timeout": {
                    "type": ["number", "null"],
                    "description": (
                        "Seconds to allow the command to run before killing it. "
                        "Defaults to no timeout."
                    ),
                },
                "env": {
                    "type": ["object", "null"],
                    "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables to set for this command.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

SEND_MESSAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": ("Send a message to the user."),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": ("Content of the message"),
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}

INVOKE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "invoke_skill",
        "description": (
            "Load a skill and return its instructions. A skill is a short guide "
            "for one kind of work, written ahead of time.\n"
            "\n"
            "Call this before starting work a skill covers, and follow what it "
            "says in place of your default approach."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The skill's directory name, for example `hello-skill`."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}

# The calculator intentionally exposes a small, structured operation instead
# of accepting Python source or an expression for eval(). This keeps the demo
# safe and makes each model action/observation easy to follow in a trajectory.
CALCULATE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "Perform one binary arithmetic operation. Chain multiple calls by "
            "using a previous result as the left or right operand."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["add", "subtract", "multiply", "divide"],
                    "description": "The arithmetic operation to perform.",
                },
                "left": {"type": "number", "description": "The left operand."},
                "right": {"type": "number", "description": "The right operand."},
            },
            "required": ["operation", "left", "right"],
            "additionalProperties": False,
        },
    },
}

SUBMIT_ANSWER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": "Return the final answer and finish the calculator task.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The final answer, including units when relevant.",
                }
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}

PLAY_MOVE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "play_move",
        "description": (
            "Play one move on the live chess board. The move must use UCI "
            "notation, for example e2e4 or e7e8q for promotion."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "move": {
                    "type": "string",
                    "description": "The legal move to play in UCI notation.",
                }
            },
            "required": ["move"],
            "additionalProperties": False,
        },
    },
}

SIMULATE_MOVE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "simulate_move",
        "description": (
            "Inspect a chess position or simulate exactly one ply without "
            "changing the live board. Supply a complete six-field FEN and, "
            "optionally, a move in UCI notation such as e2e4."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "fen": {
                    "type": "string",
                    "description": "The complete six-field FEN to inspect.",
                },
                "move": {
                    "type": ["string", "null"],
                    "description": (
                        "A UCI move to apply for one ply, or null to inspect "
                        "only the supplied position."
                    ),
                },
            },
            # Strict schemas list every property as required. Null represents
            # the optional/default value of move.
            "required": ["fen", "move"],
            "additionalProperties": False,
        },
    },
}

RUN_PYTHON_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "Run Python code inside the chess sandbox. The code can call "
            "simulate_move(fen, move=None) to search hypothetical positions "
            "and play_move(move) once to commit a move to the live board."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source code to run in the sandbox.",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}
