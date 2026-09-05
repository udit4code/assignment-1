# CodeAgent 101: a beginner's revision guide

This note explains the coding agent in [code_agent.py](../src/assignment/agent/code_agent.py). It is written for someone new to agent loops, tool calling, and state machines.

## 1. The intuition

A language model can suggest useful actions, but it does not reliably remember what it has proved, changed, or tested. A coding task therefore needs two separate responsibilities:

1. The **model** decides what action seems useful next: inspect a file, edit a file, run a test, or submit.
2. The **harness** executes that action, observes what actually happened, and decides what is allowed next.

**CodeAgent** is that harness. It is not LangChain or LangGraph. It is a custom ReAct-style agent with a small, explicit state machine. Its job is to keep the model moving through a sensible engineering workflow instead of letting it browse forever, claim success without evidence, or hide the final patch in a Git commit.

The intended journey is:

~~~
Read the problem
       |
       v
DISCOVER -- understand the bug and locate the edit
       |
       v
IMPLEMENT -- make a small, observable code change
       |
       v
VERIFY -- run a relevant test/reproduction against that change
       |
       v
SUBMIT -- capture the verified patch and finish
~~~

This is close in spirit to a LangGraph workflow. The difference is where state lives:

- In a typical LangGraph program, state is often one TypedDict or Pydantic object passed from node to node.
- Here, state is held in attributes on one long-lived CodeAgent Python object, such as self.phase, self.verification_status, and self.change_revision.

The state is not merely a prompt instruction. Python enforces it. For example, a submit call before passing verification returns a tool error; it cannot silently skip the rules.

## 2. Two layers: generic loop and coding policy

The code becomes much easier to understand when you split it into two layers.

| Layer | File | Responsibility |
| --- | --- | --- |
| Generic agent loop | src/assignment/agent/base.py | Ask the model for an action, store the response, execute tools, store observations, repeat. |
| Coding policy | src/assignment/agent/code_agent.py | Track coding phases, restrict tools, verify Git changes, and gate submission. |

CodeAgent inherits from Agent. Agent owns the generic loop; CodeAgent supplies the coding-specific tool executor, execute_tool_calls.

## 3. The actual ReAct loop

ReAct means **Reason + Act**. In this project, the model mainly expresses its next action through a tool call rather than a long natural-language answer.

Agent.run in base.py behaves like this:

~~~
while the agent is not finished:
    1. Stop if the model-call budget is exhausted.
    2. Optionally compact old conversation history.
    3. Ask the model for its next tool call.
    4. Save that assistant response in conversation history.
    5. Give the tool call to CodeAgent.execute_tool_calls().
    6. Save the resulting tool observation in conversation history.
    7. Repeat. The next model request sees the prior action and observation.
~~~

The important methods in Agent are:

| Method | Plain-English role |
| --- | --- |
| build_prompt | Combines standing instructions, the task, and past conversation into the next model prompt. |
| query_language_model | Sends that prompt to the model and increments steps_taken after a response arrives. |
| process_response | Extracts the assistant message and tool calls from the API response. |
| maybe_compact_context | Replaces older conversation with short working memory when the prompt gets too large. |
| run | Coordinates the model-action-observation cycle. |

Think of conversation_history as the model's notebook. It stores assistant calls and tool results. It is **not** the authoritative workflow state: the CodeAgent attributes are authoritative, because model text can be wrong, stale, or hallucinated.

## 4. The state machine

The CodeAgentPhase enum defines the four phases:

| Phase | Meaning | Normal next action |
| --- | --- | --- |
| DISCOVER | Root cause and edit location are not known yet. | Inspect a few relevant files or reproduce the bug. |
| IMPLEMENT | The agent should make or correct a source/test change. | Apply the smallest fix. |
| VERIFY | A real change exists and needs evidence. | Run a focused test or reproduction. |
| SUBMIT | The latest change passed verification. | Produce the final patch and summary. |

self.phase starts as DISCOVER in the constructor. The helper _transition(phase) changes phase and resets the per-phase inspection counter.

The central transitions are:

~~~
DISCOVER
  -- inspection limit reached --> IMPLEMENT

IMPLEMENT
  -- successful edit + Git sees a change --> VERIFY
  -- failed/no-op edit -------------------> IMPLEMENT

VERIFY
  -- focused test passes for current edit --> SUBMIT
  -- test fails / edit changed meanwhile --> IMPLEMENT

SUBMIT
  -- validated patch captured -----------> finished = True
~~~

In capability mode, transitions are strict. In legacy mode, the agent exposes a general execute shell tool, so it must first infer whether a command is an inspection, edit, test, or submission action.

## 5. What state is tracked?

The CodeAgent constructor initializes this state.

| State field | Question it answers |
| --- | --- |
| phase | Which workflow stage are we in? |
| steps_taken (from Agent) | How many model responses have been used? |
| phase_inspections, total_inspections | Has the agent spent too much time reading instead of acting? |
| implementation_attempts | How many edit attempts were made? |
| verification_attempts, verification_status | Did the latest verification pass, fail, or still need to happen? |
| has_successful_implementation | Has any edit produced objective evidence of a worktree change? |
| change_revision | How many confirmed edit revisions have occurred in capability mode? |
| change_digest | What exact Git diff is supposed to be verified? |
| verified_revision, verified_digest | Did that exact current diff pass a test? |
| applied_paths | Which files were edited through structured apply_patch? |
| initial_revision | Which Git commit was HEAD when the run began? |
| worktree_diff_status | Is the working tree clean, changed, or unavailable? |
| patch_created, patch_reviewed | In legacy mode, was patch.txt created and then separately checked? |
| submitted_patch, finished | What patch should the CLI save, and should the outer loop stop? |

The digest fields matter a great deal. A test passing once is not enough: an agent could edit code *after* the test. Submission compares the current Git diff's SHA-256 digest with verified_digest, rejecting a patch that differs from the one tested.

## 6. State shown to the model

_agent_state turns the key state into a compact XML-like block appended to every tool observation:

~~~xml
<agent_state phase="verify" steps_remaining="12"
  inspections="5" implementation_attempts="1"
  verification_status="required" change_revision="1"
  verified_revision="None" worktree_diff="changed">
  <next_action>Call run_tests with a focused test for the changed behavior.</next_action>
</agent_state>
~~~

This gives the model a current reminder without making it infer state from a long conversation. It also makes failures recoverable: after a rejected tool call, the model can use next_action to choose a valid next action.

_next_action generates that recommendation from the current phase and the remaining step budget.

## 7. Capability mode versus legacy mode

The constructor supports two tool interfaces:

| Interface | Tools model sees | Why it exists |
| --- | --- | --- |
| capability | inspect, apply_patch, run_tests, submit | The harness knows the intent of every action and can enforce phases precisely. |
| legacy | execute, send_message, plus optional skills | Compatible with the earlier flexible shell-based assignment workflow. |

For a controlled new evaluation, capability mode is the clearest mental model:

~~~
inspect -> apply_patch -> run_tests -> submit
~~~

Legacy mode needs more defensive code because execute can be anything. _classify_command makes a conservative guess:

| Detected command type | Classified as |
| --- | --- |
| rg, sed -n, git status, git diff, ls | INSPECT |
| pytest, python manage.py test, make test | VERIFY |
| redirects, sed -i, git apply, package installs | IMPLEMENT |
| creating patch.txt or git format-patch | SUBMIT |
| unknown command | IMPLEMENT, which is safer than blocking a project-specific edit helper |

ExecuteKind classifies one command. CodeAgentPhase is the longer-lived workflow state. They are related but not the same thing.

## 8. How the main CodeAgent methods participate in control flow

### Setup and safety helpers

| Method | What it does | Why it matters |
| --- | --- | --- |
| __init__ | Initializes state, selects tools, builds the system prompt, and requires a tool call each turn. | Establishes the rules before the first model request. |
| _command_text | Turns string/list shell commands into normalized text. | Lets later checks handle both command formats. |
| _contains_forbidden_git_action | Detects model-issued Git commands such as add, commit, reset, and checkout. | Prevents destructive or patch-hiding Git operations. |
| _classify_command | Categorizes a legacy execute command. | Lets the state machine apply rules to a generic shell tool. |
| _safe_repo_path | Rejects absolute paths, .., NUL bytes, and option-like paths. | Keeps structured file operations inside the repository. |
| _tool_error | Produces a consistent error observation. | A tool error teaches the model what to do differently instead of crashing the loop. |

### Phase and budget helpers

| Method | What it does | Why it matters |
| --- | --- | --- |
| _transition | Sets a new phase and resets local inspection counting. | Centralizes phase changes. |
| _next_action | Generates a phase-aware recommendation. | Reduces wasted turns after an error or near the step limit. |
| _agent_state | Serializes state plus the recommendation into every observation. | Makes control state visible to the model. |
| _admit_command | Allows or rejects a proposed legacy action before execution. | Enforces inspection quotas and prevents early submission. |
| _record_command_result | Updates legacy-mode state after a command actually runs. | State changes follow observed results, not a model claim. |

### Objective Git evidence helpers

| Method | What it does | Why it matters |
| --- | --- | --- |
| _ensure_initial_revision | Records git rev-parse HEAD once before the first legacy shell action. | Gives a stable baseline for the run. |
| _baseline_diff | Diffs the worktree against that starting revision. | Detects a real source change even if a command merely looked like an edit. |
| _working_tree_changed | Interprets Git status/diff results and ignores submission-only patch.txt. | Prevents patch.txt from being mistaken for implementation progress. |
| _format_git_status | Formats git status --porcelain for the model. | Gives transparent evidence after every action. |
| _format_baseline_diff | Reports whether a baseline diff exists, its size, and a digest, not the entire diff. | Supplies progress evidence without growing context too much. |
| _cached_diff | Reads the staged diff for structured capability paths. | Binds verification and submission to the exact edited files. |
| _result_digest | Hashes a successful diff result. | Detects changes made after testing. |
| _status_observation | Combines command output and Git status. | Ensures the model sees task output and repository state together. |

### Capability-mode handlers

execute_tool_calls routes a tool call to one of these handlers when tool_interface is capability.

| Handler | Input | State effect |
| --- | --- | --- |
| _handle_inspect | List files, read bounded lines, search literal text, or read a Git diff. | Charges an inspection quota; otherwise keeps the current phase. |
| _handle_apply_patch | Exact path, old_text, and new_text. | Replaces text only if old_text occurs exactly once; confirms a Git change; then moves to VERIFY. Failure remains in IMPLEMENT. |
| _handle_run_tests | argv plus optional cwd, environment, and timeout. | Passing test plus unchanged diff moves to SUBMIT. Failure or changed worktree returns to IMPLEMENT. |
| _handle_submit | Concise summary. | Re-checks status and verified diff digest, stores submitted_patch, then sets finished = True. |

One implementation detail: capability mode uses git add **inside the harness** to get a precise cached diff for controlled paths. The model itself is forbidden from issuing git add or history-changing commands. Legacy mode keeps model changes uncommitted and creates its patch from the worktree diff.

## 9. The dispatcher: where everything meets

execute_tool_calls is the bridge between an untrusted model proposal and a real sandbox action.

For every tool call, it does this:

~~~
1. Validate the call shape: dictionary, function, valid JSON, and object arguments.
2. In capability mode, dispatch inspect / apply_patch / run_tests / submit.
3. In legacy execute mode:
      a. validate execute arguments,
      b. reject forbidden Git mutations,
      c. classify the command,
      d. ask the phase gate whether it is allowed,
      e. execute only if allowed,
      f. run Git status and diff internally,
      g. update state from observations,
      h. return command output + Git evidence + agent state.
4. In legacy send_message mode, require all submission prerequisites.
5. For a skill call, validate the skill and phase-gate it if necessary.
6. Append a fresh agent-state block to the returned observation.
~~~

That observation goes back to Agent.run, which stores it in conversation_history. The following model request sees exactly what happened.

## 10. A concrete successful run

Suppose the task is: “The server returns HTTP 500 when a human chess move ends the game. Fix it.” An ideal capability-mode sequence is:

~~~
Step 1  inspect(read_file game.py)              phase: DISCOVER
Step 2  inspect(search move handling)           phase: DISCOVER
Step 3  apply_patch(game.py, old, new)          Git confirms change
                                                phase: VERIFY
Step 4  run_tests(["pytest", "..."])            test passes; diff unchanged
                                                phase: SUBMIT
Step 5  submit("Guarded engine reply...")       patch captured; finished=True
~~~

The model chooses the calls, but it cannot perform Step 5 at Step 1. The submission handler checks the phase, verified revision number, and Git diff digest before accepting the patch.

## 11. What happens when the model goes off track?

| Model behavior | Harness response |
| --- | --- |
| Inspects seven times when the limit is six | Rejects the seventh call with “Inspection budget exhausted”; no command runs. |
| Says “I fixed it” but did not alter a file | Git sees no change, so the agent remains in IMPLEMENT. |
| Runs a test before editing | The result is only baseline evidence; it cannot unlock submission. |
| Edits after a passing test | Current diff no longer matches verified_digest, so submission is rejected. |
| Runs git commit or git reset | The command is rejected before it runs. |
| Sends a final answer too early in legacy mode | send_message is rejected until skill/patch checks complete. |
| Repeats an identical legacy command | The observation includes a progress warning with the repeat count. |

The central principle is: **the model proposes; the harness proves and permits.**

## 12. Best source reading order

When revising, read the source in this order:

1. CodeAgentPhase and ExecuteKind: vocabulary for state and action kinds.
2. __init__: initial state, tools, and standing policy.
3. _transition, _next_action, _agent_state: visible state machine.
4. _admit_command and _record_command_result: legacy-mode gates and transitions.
5. _handle_inspect, _handle_apply_patch, _handle_run_tests, _handle_submit: strict capability workflow.
6. execute_tool_calls: dispatcher that ties all parts together.
7. Agent.run in base.py: outer loop that repeats until finished is true or the step limit is reached.

## 13. Quick revision answers

**Where is state?**

Mostly in CodeAgent instance attributes. Conversation history is model context, not the sole source of truth.

**What causes a phase change?**

Observed evidence: an inspection budget being reached, Git confirming an edit, a test passing/failing, or valid submission. A model sentence alone does not change phase.

**Why run git status --porcelain internally?**

Because an action that looks like an edit may fail, edit the wrong thing, or only create a patch file. Git is the objective progress sensor.

**Why tie tests to a digest?**

To ensure submitted code is the same code that passed. Otherwise an agent could test version A and submit untested version B.

**Why use a step limit and inspection budget?**

LLM agents can keep gathering information indefinitely. These limits reserve actions for actual completion: editing, verifying, and submitting.

**Is this LangGraph?**

No. It uses the same important idea—explicit state plus guarded transitions—but writes the graph procedurally in Python methods instead of declaring LangGraph nodes and edges.

