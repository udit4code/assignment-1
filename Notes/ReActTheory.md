# ReAct: Theory from First Principles

## 1. Why do we need an agent harness?

A language model receives some input and predicts a useful continuation. By
itself, it cannot inspect a directory, run a test, make an HTTP request, or
change a file. It can only produce data—usually text or a structured message.

For example, a model may produce this sentence:

```text
I should inspect the repository.
```

That sentence does not actually inspect anything. Some ordinary program must
interpret the model's request, run the operation, and return the result.

That ordinary program is the **agent harness**.

The main pieces are:

```text
  (1) USER
      "What is 27 + 15?"
              |
              v
  (2) AGENT HARNESS
      Builds a prompt containing:
      - the user's task;
      - conversation history;
      - available tool schemas.
              |
              v
  (3) LANGUAGE MODEL
      Chooses the next response. It may return a structured tool call:

      tool: addition_operation
      arguments: {"a": 27, "b": 15}
              |
              v
  (4) AGENT HARNESS
      - parses the structured call;
      - checks that the tool and arguments are valid;
      - dispatches the call to the registered implementation.
              |
              v
  (5) TOOL / ENVIRONMENT
      Runs the real function: addition_operation(a=27, b=15)
      Produces the real result: 42
              |
              v
  (6) AGENT HARNESS
      Adds a structured tool-result message to the history:

      tool result: 42
              |
              v
  (7) LANGUAGE MODEL
      Receives the updated history and answers:
      "27 + 15 = 42."
```

The cycle can also be shown more compactly:

```text
User
  |
  | task
  v
Harness ---- prompt + tool schemas ----> Language model
   ^                                      |
   |                                      | structured tool call
   |                                      v
   +---- structured observation ---- Tool dispatcher
                                          |
                                          | validated function call
                                          v
                                    Tool / environment
```

The language model decides what it would like to do. The harness controls what
it is actually allowed to do and performs the real operation.

### A crucial distinction: prose is not a tool call

Suppose the model produces only ordinary text:

```text
I should invoke the addition operation with 27 and 15.
```

The harness should normally treat that as text. It should **not** guess that it
must execute a function. Guessing executable intent from prose would be
ambiguous and unsafe.

To request execution, the model should return the structured form required by
the API, conceptually:

```json
{
  "id": "call_1",
  "type": "function",
  "function": {
    "name": "addition_operation",
    "arguments": "{\"a\": 27, \"b\": 15}"
  }
}
```

The model chooses `addition_operation` and supplies its proposed arguments
because the harness previously showed the model that tool's schema. The
harness then parses and validates the proposal, finds the corresponding real
function, executes it, and returns the result as a structured observation.

## 2. Model, agent, harness, tool, and environment

These terms are related, but they are not interchangeable.

### Language model

The language model maps an input sequence to an output sequence. It does not
automatically retain state between API requests. If an earlier event matters,
the harness normally has to include that event in a later request.

### Agent

An agent is a system that uses a model to pursue an objective over multiple
steps. It can observe results, revise its approach, and choose another action.

### Agent harness

The harness is the regular software surrounding the model. It is responsible
for:

- constructing prompts;
- storing conversation state;
- declaring available tools;
- validating model-generated tool calls;
- executing permitted actions;
- returning observations to the model;
- detecting completion and enforcing limits;
- logging the trajectory;
- cleaning up resources.

The harness, not the model, is the component with real authority.

### Tool

A tool is a controlled operation exposed to the model. Examples include:

- `execute(command)` to run a terminal command;
- `play_move(move)` to send a chess move to a server;
- `invoke_skill(name)` to load additional instructions;
- `send_message(summary)` to finish and report a result.

A tool has two important parts:

1. A **schema** that tells the model the tool's name, purpose, and arguments.
2. An **implementation** that validates the arguments and performs the action.

The schema is a description, not the executable function itself.

### Environment

The environment is the world in which actions occur. For a coding agent, it
might be an isolated Linux container containing a Git repository. For a chess
agent, it might include a running chess server.

Isolation matters because model-generated commands are untrusted input. A
coding agent should work inside a sandbox instead of directly on the host Mac.

## 3. What is ReAct?

**ReAct** stands for **Reasoning and Acting**. Its central idea is to alternate
between deciding what to do, acting in an environment, and observing the
result.

A simplified cycle is:

```text
Reason -> Act -> Observe -> Reason -> Act -> Observe -> ... -> Finish
```

Historically, ReAct examples often used explicit `Thought`, `Action`, and
`Observation` text. Modern APIs usually represent actions as structured tool
calls. A model may also keep some reasoning internal. The essential idea is
the feedback loop, not a requirement to expose private chain-of-thought.

### Why is the loop useful?

Without observations, the model must guess. Suppose it is asked to repair a
test failure. A one-shot answer would have to guess:

- which file contains the bug;
- how the current code works;
- what error the tests produce;
- whether the proposed edit fixes the problem.

With ReAct, the agent can gather evidence:

```text
Inspect files
    -> observe filenames
Read likely source
    -> observe implementation
Run failing test
    -> observe traceback
Edit source
    -> observe command result
Run test again
    -> observe success or another failure
Submit patch
```

Each new decision is informed by the real result of the previous action.

## 4. One ReAct step in detail

Imagine the user asks:

```text
Fix the function that incorrectly adds two numbers.
```

The harness sends the model a prompt containing the task and available tools.
The model might return a structured tool call:

```json
{
  "id": "call_1",
  "type": "function",
  "function": {
    "name": "execute",
    "arguments": "{\"command\": \"rg -n 'def add' .\"}"
  }
}
```

Important detail: `arguments` is commonly a JSON-encoded **string**. The
harness must parse it before using it.

The harness validates the call and executes the command in the sandbox. It
might receive:

```text
src/calculator.py:8:def add(a, b):
```

The harness turns this result into a tool message:

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "content": "src/calculator.py:8:def add(a, b):"
}
```

The `tool_call_id` links the observation to the action that produced it. This
is especially important if an assistant message contains multiple calls.

On the next iteration, the harness sends the model:

- the original instructions;
- the original task;
- the assistant's `call_1` action;
- the tool observation linked to `call_1`.

The model can now decide to read `src/calculator.py`. This is one complete
action-observation feedback cycle.

## 5. The message roles

This assignment uses an OpenAI-compatible chat format. The main roles are:

### `system`

Standing instructions and constraints. Examples:

- what kind of agent this is;
- what environment it is operating in;
- safety or submission rules;
- brief metadata about available skills.

Normally there is one system message at the beginning.

### `user`

The objective the agent must accomplish, such as a software issue or the
instruction to play a chess game.

### `assistant`

The model's response. It may contain text, tool calls, or both. The harness
must preserve the assistant message so the next request includes what the
model attempted.

### `tool`

The result of executing one tool call. It must be linked to the corresponding
assistant call using `tool_call_id`.

A typical prompt after one action looks like:

```text
system: standing instructions
user: original task
assistant: call execute("pytest ...")
tool: output and exit code for that call
```

## 6. State: how the agent remembers

API calls are separate requests. The model does not magically remember the
previous request. The harness therefore maintains a history.

Conceptually:

```python
history = []

history.append(assistant_message)
history.extend(tool_observations)
```

Then prompt construction is approximately:

```python
prompt = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": task_prompt},
    *history,
]
```

There are many valid internal representations. What matters is that later
model requests contain enough valid history to understand what happened.

Two histories in this assignment should not be confused:

- **Active conversational state** is what the next model request sees.
- **API logs/trajectory** record requests and responses for later evaluation.

Compaction may change active state, while the complete trajectory should still
record what actually happened.

## 7. The generic ReAct loop

A domain-independent loop can be expressed as:

```python
while not finished:
    if steps_taken >= step_limit:
        raise StepLimitError

    maybe_compact_context()

    assistant_message = query_language_model()
    save_to_history(assistant_message)

    tool_calls = assistant_message.get("tool_calls", [])
    observations = execute_tool_calls(tool_calls)
    save_all_to_history(observations)
```

This loop belongs in the base agent because both a coding agent and a chess
agent follow the same control pattern. Their tools differ, but the loop does
not.

The subclass supplies domain-specific behavior:

```text
CodeAgent                 ChessAgent
---------                 ----------
execute                    play_move
send_message               simulate_move
invoke_skill               run_python
                           invoke_skill
```

## 8. Completion and step limits

An agent needs an explicit stopping condition. Otherwise, it can continue
calling the model forever.

In this assignment, a domain tool can mark the task complete:

- The coding agent finishes after its completion/submission message.
- The chess agent finishes when the returned game state says `game_over`.

A `step_limit` is a second line of defence. It bounds cost and prevents an
agent stuck in a loop from running indefinitely.

The usual distinction is:

```text
finished == True       -> successful or natural termination
step limit exhausted  -> raise StepLimitError
```

The step count refers to model action requests, not the number of shell
commands. One model response can contain zero, one, or multiple tool calls.

## 9. Errors should often become observations

A robust harness distinguishes two categories of failure.

### Recoverable action errors

Examples:

- malformed JSON arguments;
- an unknown tool name;
- an illegal chess move;
- a shell command returning exit code 1;
- an invalid file path.

These should usually be returned to the model as observations. The agent can
then correct its action.

```text
Action: play e2e5
Observation: illegal move
Next action: choose a legal move
```

If the harness crashes on every model mistake, it is not much of an agent
harness.

### Terminal infrastructure errors

Examples:

- the sandbox has disappeared;
- the model service is unreachable after retries;
- required setup is missing;
- the trajectory cannot be saved.

Continuing may be impossible or unsafe, so these errors may need to propagate.

Even when a run fails, cleanup and trajectory saving should happen in a
`finally` block.

## 10. Tool validation is a security boundary

Never assume a tool call is correct merely because a model generated it. The
harness should validate:

- whether the tool name is registered;
- whether `arguments` contains valid JSON;
- whether the decoded value is an object;
- whether required fields exist;
- whether values have the expected types;
- whether extra arguments are allowed;
- whether multiple calls are safe to execute together.

For example, two parallel `play_move` calls cannot both operate on the same
position. After the first move, the live board has changed. A safe chess
harness executes at most one and reports the others as recoverable errors.

Tool schemas help the model produce valid calls, but server-side validation in
the harness is still required.

## 11. Context growth and compaction

Every observation added to history makes later prompts longer. Long prompts:

- use more tokens;
- take more time;
- may exceed the model's context window;
- can bury important facts in old terminal output.

Simple truncation is useful for a single huge observation. For example, the
harness can keep the beginning and end of a long terminal result and omit its
middle.

**Context compaction** addresses the entire growing history. It replaces an
old prefix with concise working memory while keeping recent interactions in
their original form.

Conceptually:

```text
Before:
system + task + old action/result pairs + recent action/result pair

After:
system + task + summary of old work + recent action/result pair
```

A useful software-agent summary preserves:

- the objective and constraints;
- relevant files and symbols;
- commands already run;
- edits already made;
- concrete test results;
- failed approaches and why they failed;
- blockers;
- the next intended action.

Compaction is lossy. It saves context space but may remove a detail that later
turns out to matter. Keeping at least one recent complete assistant/tool pair
also keeps the message sequence valid and gives the model precise recent
evidence.

## 12. Skills and progressive disclosure

A skill is a reusable set of instructions for a particular workflow. Loading
the full text of every available skill into every prompt would waste context.

Progressive disclosure solves this in stages:

1. The system prompt lists compact skill metadata, such as name and
   description.
2. The model recognizes that a skill is relevant.
3. The model calls `invoke_skill(name)`.
4. The harness returns the complete skill instructions as an observation.

In this assignment, the coding agent initially learns that a `submit-task`
skill exists. It should not see the skill's full submission instructions until
it invokes that skill.

## 13. How the theory maps to this repository

| Concept | Repository location |
|---|---|
| Generic state, prompt, loop, and compaction | `src/assignment/agent/base.py` |
| Coding prompts and terminal-tool dispatch | `src/assignment/agent/code_agent.py` |
| Tool schemas shown to the model | `src/assignment/agent/tools.py` |
| Chess-specific dispatch and state updates | `src/assignment/agent/chess_agent.py` |
| Implementations of chess operations | `src/assignment/agent/chess_tools.py` |
| Sandbox execution interface | `src/assignment/env.py` |
| Fake-model and fake-environment specifications | `tests/test_agent.py` |
| Chess-tool behavior specifications | `tests/test_chess_agent.py` |

The most important design rule is:

> Put the reusable ReAct control loop in `Agent`; put domain-specific tool
> behavior in `CodeAgent` and `ChessAgent`.

## 14. Unit tests versus end-to-end tests

The offline unit tests use scripted model responses and fake environments.
They answer questions such as:

- Does the second prompt contain the previous action and observation?
- Are long observations truncated?
- Does malformed JSON become a recoverable observation?
- Does the step limit stop the loop?
- Is a chess move linked to the correct call ID?

They do not prove that:

- a real local model is capable enough to solve the task;
- the Ollama request and response formats work end to end;
- a Colima container starts correctly;
- an agent-generated patch fixes the real application;
- cleanup, networking, port forwarding, and architecture emulation work.

That is why the project has separate run and evaluation stages. First verify
the harness deterministically with unit tests. Then use the real model and
sandbox. Finally, evaluate the artifact the agent produced in a fresh
environment.

## 15. Common beginner mistakes

### Treating model text as an executed action

The model only proposes an action. The harness must parse, validate, and run
it.

### Forgetting to save the assistant message

Saving only the tool output produces an incomplete conversation. The next
prompt needs both the call and its linked result.

### Returning an unlinked observation

Every tool result should contain the original `tool_call_id`.

### Letting bad JSON crash the loop

Malformed model output is expected occasionally. Turn it into a useful error
observation when recovery is possible.

### Executing model commands on the host

Model-generated shell commands belong in a disposable sandbox, not directly
on your laptop.

### Mixing domain logic into the base loop

The base agent should not know how Bash or chess works. It should only know how
to request actions, record them, dispatch them through an abstract method, and
continue or stop.

### Assuming passing unit tests proves agent quality

Unit tests can prove that messages and tools are wired correctly. They cannot
guarantee that a particular model will reason well enough to solve a new task.

## 16. A compact revision model

Remember ReAct using five verbs:

```text
PROMPT -> PROPOSE -> VALIDATE -> EXECUTE -> OBSERVE -> repeat
```

Or remember the responsibilities this way:

```text
Model:   chooses a proposed next action
Harness: remembers, validates, controls, and stops
Tool:    exposes one permitted capability
Sandbox: contains the effects of the action
History: gives the next model call its working context
```

## 17. Revision questions

You should be able to answer these after revising this note:

1. Why can a language model not run a terminal command by itself?
2. What is the difference between a tool schema and a tool implementation?
3. Why must a tool result contain `tool_call_id`?
4. Why must the harness save both assistant actions and tool observations?
5. Which errors should become observations instead of exceptions?
6. Why does a ReAct agent need a step limit?
7. Why is the shared loop implemented in the base agent?
8. What information should context compaction preserve?
9. What does progressive disclosure accomplish for skills?
10. Why are passing unit tests necessary but insufficient for an end-to-end
    agent run?

If these answers are clear, you have the conceptual foundation needed to begin
`Agent.build_prompt()` and then implement the ReAct loop.
