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

## 3. Where the model, agent, sandbox, and Git changes run

Yes: your mental model is correct, with one important separation. The Python
program that constructs and controls the agent runs on **your machine**. It
calls a model endpoint for reasoning, and sends repository commands to a
separate, disposable testbed.

~~~text
Your Mac / editor workspace
├── runs: uv run assignment-code-agent ...
├── runs: Agent.run() and CodeAgent Python code
├── calls: model API for the next tool action
├── writes: artifacts/trajectory.json and artifacts/fix.patch
└── does NOT receive the sandbox's source-file edits directly

Model service
├── OpenAI API: remote service over HTTPS
└── Ollama: usually a local server on your Mac, over http://127.0.0.1:11434

Disposable testbed, with its own /testbed repository
├── Modal backend: a remote Modal sandbox
└── Docker backend: a local Docker container using the active Docker context
    (for you, that can be Colima)
~~~

### What happens in one action

1. The host-side Python process calls the LLM API with the prompt, tool
   definitions, and previous observations.
2. The LLM returns a requested tool action, such as an inspection, edit, or
   test command. It does not directly touch any file.
3. CodeAgent validates and gates that action.
4. The environment object executes the allowed action in `/testbed` inside the
   Modal sandbox or Docker/Colima container.
5. CodeAgent reads the command result plus Git evidence from that same sandbox
   repository and adds it to the next model observation.

With an OpenAI model, reasoning is remote but code execution is still in the
selected sandbox. With Ollama, reasoning usually happens locally on your Mac,
but code execution is still in the selected sandbox. Model location and code
execution location are independent choices.

### Why your editor's Git repository is unchanged

The repository the agent edits is **not** your editor workspace. Before a run,
the CLI builds an isolated testbed image from the task source and then starts
it with `/testbed` as the working directory. The local Docker implementation
explicitly does not mount a host directory or Docker socket into the container.
The Modal implementation similarly runs a separate remote sandbox.

So this is expected:

~~~text
Terminal in your editor:     git status / git log for your host checkout
Agent inside the sandbox:    git status / git diff for the disposable /testbed checkout
~~~

They are different Git repositories and different filesystems. A source change
inside the sandbox cannot appear as a modified file in your editor.

Also, `git log` shows **commits**, not ordinary edits. The agent deliberately
forbids the model from committing because a commit can hide the required patch
from a working-tree diff. Therefore, even if you opened a shell in the live
sandbox, `git log` would normally not gain a new commit. The useful diagnostic
there would be `git status --porcelain` and `git diff`.

There is one implementation nuance:

- In **legacy mode**, model-made edits remain uncommitted in the sandbox worktree.
- In **capability mode**, the harness internally stages the controlled files
  with `git add` so it can calculate a precise cached diff. It still does not
  create a commit, and this staging exists only inside the disposable sandbox.

When the run succeeds, the host-side CLI copies only two durable outputs out of
that environment:

| Host artifact | Contents |
| --- | --- |
| trajectory JSON passed through `--trajectory` | Prompts, model responses, and compaction records. It is a log, not a live checkout. |
| patch passed through `--patch-output` | The submitted Git diff stored in `agent.submitted_patch`. This is the portable representation of the sandbox edit. |

After the `with environment_class(...)` block ends, the testbed is stopped and
removed. That is why the patch artifact is the result to inspect, apply, and
evaluate later; it is not expected that the source edits survive as files in
your working directory.

To apply a successful sandbox result to a separate local checkout, you would
do that intentionally from the host using the saved patch, for example
`git apply artifacts/openai-fix.patch` in the repository you want to change.
Do this only when you want to modify that checkout; it is deliberately not part
of the agent run.

## 4. Skills: reusable playbooks, not executable tools

This is a very common point of confusion, so start with this sentence:

> A **tool** lets an agent *do* something. A **skill** teaches an agent *how
> and when to use tools* for a reusable kind of work.

Think of an agent as a new engineer joining a team.

| Thing | Everyday analogy | In an agent |
| --- | --- | --- |
| Tool | A keyboard, terminal, test runner, or screwdriver | An executable function the model can call, such as `execute`, `inspect`, `run_tests`, or `play_move`. |
| Skill | A runbook, checklist, or standard operating procedure | A `SKILL.md` instruction document explaining a repeatable workflow. |

A screwdriver can turn a screw, but it does not tell a new engineer *which*
screw to turn, in what order, or how to check the result. A skill is that
procedure. It has no direct ability to edit a file, run a test, or call an API.
It only gives the model better instructions; the model must still call tools to
perform the work.

### Tools versus skills, precisely

| Property | Tool | Skill |
| --- | --- | --- |
| Where it is defined here | `src/assignment/agent/tools.py` | A `SKILL.md` file in a child folder of a skills directory. |
| What the model receives | A function name, description, and JSON argument schema. | A name, short description, then full Markdown instructions when loaded. |
| What happens when used | The Python harness executes code and returns an observation. | The harness returns the skill text to the model; no work happens by itself. |
| Has side effects? | It can. For example, an edit tool changes a file and a test tool runs code. | No. Reading a skill changes only the model's available guidance. |
| Example | `run_tests({"argv": ["pytest"]})` | `invoke_skill({"name": "submit-task"})` |

The `invoke_skill` call is itself a **tool**. It is a read-only tool whose
result happens to be the contents of a skill. This is the source of much of the
terminology confusion:

~~~text
SKILL.md                  invoke_skill tool                 normal tools
instructions on disk  ->  loads instructions into context ->  carry out instructions
runbook                    “please show me the runbook”       execute / run_tests / send_message
~~~

### What a SKILL.md file looks like

A skill is a folder containing a file called `SKILL.md`. The top of the file is
YAML frontmatter between `---` lines. It provides the small catalog entry. The
rest is normal Markdown containing the complete playbook.

The coding-agent skill in this assignment is:

~~~text
tasks/code-skills/
└── submit-task/
    └── SKILL.md
~~~

Its frontmatter is conceptually:

~~~yaml
name: submit-task
description: When the task is complete, take the required steps to submit it.
~~~

Its body explains the required submission protocol:

1. Create `patch.txt` with a Git diff containing only the intended source-file
   changes.
2. Read and inspect `patch.txt` separately.
3. Call `send_message` with a final summary.

Notice what is missing: `SKILL.md` does not run `git diff`, create the file, or
send the message. It tells the model that it must use the `execute` and
`send_message` tools to make those things happen.

### Why skills are useful: progressive disclosure

You could paste every possible runbook into the system prompt. That becomes
expensive and confusing as the number of workflows grows. The agent may be
repairing a bug, reviewing a migration, investigating an incident, or
submitting a patch; it should not need the detailed instructions for all of
those jobs on every model call.

Skills solve this with **progressive disclosure**:

~~~text
Level 1: system prompt shows a compact catalog
         “submit-task: When the task is complete, take required submission steps.”

Level 2: model decides the work now needs that workflow
         -> invokes invoke_skill("submit-task")

Level 3: tool returns the full SKILL.md instructions
         -> model follows the listed steps with ordinary tools
~~~

This saves prompt space, keeps the default instructions focused, and lets one
agent support many specialised workflows without changing its core Python code.
It also makes team procedures reusable: improving one skill file improves every
agent that loads it.

### Exact skill lifecycle in this assignment

Here is the real end-to-end path for the Part 1 coding agent in legacy mode.

~~~text
1. Makefile runs assignment-code-agent with:
      --skills-path tasks/code-skills

2. Agent.__init__ calls load_skills(skills_path).
   It scans child folders, reads each SKILL.md, validates its YAML frontmatter,
   and creates self.skills, keyed by skill name.

3. Because self.skills is non-empty, Agent exposes the invoke_skill tool.
   CodeAgent adds the name and description of every available skill to its
   system prompt, but not the full body.

4. After implementation and verification, the model calls:
      invoke_skill({"name": "submit-task"})

5. CodeAgent validates the name and phase, records it in self.invoked_skills,
   and returns the complete SKILL.md text as a tool observation.

6. The model now follows the loaded instructions using ordinary tools:
      execute -> create patch.txt
      execute -> read patch.txt
      send_message -> finish with a summary

7. CodeAgent enforces the protocol. It refuses early submission until the skill
   was invoked, patch.txt exists, and patch.txt was reviewed in a separate step.
~~~

So the skill provides the *recipe*, while CodeAgent provides the *guardrails*
that make ignoring the recipe difficult.

### Where we used skills in this repository

There are two assignment examples:

| Agent | Skills directory | Skill | Purpose |
| --- | --- | --- | --- |
| CodeAgent, legacy interface | `tasks/code-skills` | `submit-task` | Teaches the three-step patch submission protocol. |
| ChessAgent | `tasks/chess-skills` | `select-move` | Teaches a deterministic chess-search strategy: simulate candidate moves, score them, then commit exactly one live move. |

The Makefile's `run-code-agent` and `run-swebench-agent` targets pass
`--skills-path tasks/code-skills`, so their normal legacy runs make
`submit-task` available. The chess run passes `tasks/chess-skills`.

There is one deliberate exception. The newer **capability interface** exposes
explicit `inspect`, `apply_patch`, `run_tests`, and `submit` tools. In that
mode, CodeAgent removes the legacy `submit-task` skill from the visible catalog.
The harness-owned `submit` tool already creates and validates the patch, so a
separate `patch.txt` runbook would be redundant and could conflict with the
stricter flow.

### A tiny worked example

Suppose the model has fixed code and the agent is in SUBMIT phase.

~~~text
Without a skill:
  Model knows it can call execute and send_message, but must infer the required
  patch format and the separate review step from the general prompt.

With submit-task:
  Model invokes the skill.
  It receives a precise checklist: create patch.txt, read it, then send summary.
  It calls execute twice and send_message once.
  The harness verifies those steps before marking the run complete.
~~~

The skill does not make the model smarter in the sense of changing model
weights. It makes success more likely by placing the right task-specific
procedure in the model's context at the right time.

### The one-line revision rule

**Tools are verbs; skills are playbooks.**

The model calls a tool to act. The model loads a skill to learn a process, then
uses tools to carry out that process.

## 5. The actual ReAct loop

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

## 6. Systems engineering view: latency, resources, and failures

An agent loop is a small distributed system. From the moment a user starts a run until it returns a final result, work crosses several machines and failure domains. A Principal Engineer asks more than “Is the model smart enough?”:

- Where does wall-clock time go?
- Which component is the bottleneck?
- Which resource is saturated: GPU, CPU, memory, disk, or network?
- Which failures are safe to retry, and which invalidate the result?
- What evidence must be recorded to debug the run later?

### End-to-end timeline

For this assignment, a coding-agent run follows this path:

~~~text
User starts command
  |
  +--> Host-side setup
  |      parse CLI, load task and skills, build/locate testbed image,
  |      start Modal sandbox or Docker/Colima container
  |
  +--> Repeat for each agent step
  |      1. Build prompt from task, conversation, and agent state
  |      2. Send model request; wait for reasoning and a tool call
  |      3. Validate, classify, and phase-gate the requested tool call
  |      4. Run the allowed command in the sandbox
  |      5. Collect stdout/stderr, Git status/diff, and tool result
  |      6. Add observation and state to the next model prompt
  |
  +--> Completion
         capture patch, write trajectory/patch artifacts on the host,
         stop the sandbox, return the outcome
~~~

A useful approximation is:

~~~text
Total wall-clock time
  = setup + sandbox start
  + sum for each agent step of (
        prompt preparation
      + model-service latency
      + harness policy/dispatch
      + sandbox command execution
      + result transfer and observation formatting)
  + artifact writing + cleanup
~~~

These terms are mostly sequential in the current implementation. The agent waits for the model, then waits for the chosen tool, then asks the model again. One slow model response or one slow test is directly visible to the user.

### What each component does

| Component | Work done here | Dominant resource | Typical latency driver |
| --- | --- | --- | --- |
| Host-side agent process | Python builds prompts, validates JSON, runs phase gates, hashes diffs, writes artifacts. | Usually light CPU, memory, and local disk I/O. | Usually milliseconds; large histories and diffs make serialization/hashing noticeable. |
| Model transport | Host sends a prompt/tool schemas to OpenAI or Ollama and receives a response. | Network I/O for remote service; local HTTP I/O for Ollama. | Network round trip, provider queueing, request size. |
| LLM inference | Model reads prompt, reasons, and emits a tool call. | Usually GPU/accelerator-heavy; CPU/RAM-heavy if a local model cannot use an accelerator. | Prompt length, reasoning effort, generated tokens, model size, provider contention. |
| Harness dispatch | CodeAgent validates state and creates observations. | Light CPU. | Normally negligible; grows with output/diff size. |
| Sandbox control plane | Host asks Modal/SWE-ReX or Docker/Colima to run a command. | Network I/O for Modal; local IPC/VM boundary for Docker/Colima. | Remote tunnel latency, container scheduling, Docker daemon health. |
| Command in /testbed | Inspection, edit, test, build, or patch command executes. | Depends: CPU, RAM, disk I/O, and sometimes network. | Test suite size, interpreter startup, dependency state, filesystem size. |
| Sandbox lifecycle | Image build/pull, container startup, shutdown, cleanup. | CPU + disk + network for image work; remote scheduling for Modal. | Cold cache, image size, platform emulation, provider capacity. |

### GPU, CPU, and I/O: a beginner-friendly model

#### LLM reasoning is usually the GPU-heavy part

For the OpenAI API, inference hardware is operated remotely by the provider. You do not schedule individual GPUs; you observe their effects through response latency, usage, queueing, and errors.

For local Ollama, inference may use a local GPU, Apple Silicon acceleration, or CPU depending on your model, hardware, and configuration. A large model that cannot fit in available accelerator memory may fall back partly or fully to CPU and become much slower.

Inference has two important parts:

| Stage | Meaning | What makes it slower |
| --- | --- | --- |
| Prompt processing, often called prefill | The model reads system instructions, task, skills, and past observations. | Long conversation, verbose tool output, many tools, large skill bodies. |
| Token generation, often called decode | The model emits reasoning and its next tool call. | Long output, high reasoning effort, large model, slow hardware, provider load. |

This is why bounded tool output, inspection budgets, and context compaction are systems optimizations as well as agent-quality optimizations. Smaller prompts generally mean lower latency and lower cost.

#### Tests and builds are usually CPU/RAM-heavy

Pytest, compilers, static analysis, and simulations consume sandbox CPU time and memory. A large parallel test suite can be CPU-bound. Test discovery or reading a large repository can be disk-I/O-bound. Python tests often mix CPU, disk I/O, process startup, and imports.

The local Docker sandbox limits a container to two CPUs, two GiB of memory, and 512 processes. This improves isolation, but it means a test that is fast on a powerful host can be slower or memory-constrained inside the testbed.

#### Remote boundaries are usually I/O-bound

With Modal, the host waits over the network for sandbox creation and command results. With Docker on Colima, control remains local but crosses the Docker CLI and VM boundary. Image pulls/builds can spend most of their time waiting on registries and disk layers rather than executing Python code.

#### The harness is CPU-bound, but should remain small

Prompt serialization, JSON parsing, regular expressions, Git-diff hashing, and trajectory writing consume host CPU. They become noticeable only with very large histories or command outputs. Output truncation and compact Git evidence keep this control-plane work predictable.

### Cold path versus warm path

Do not time one run and assume every run will take the same time.

| Path | Includes | Why it differs |
| --- | --- | --- |
| Cold path | Build/pull image, start sandbox, create remote runtime, normal agent loop. | Empty cache, image layers, provider scheduling, and container/VM setup add substantial time. |
| Warm path | Cached image layers/model availability, then normal loop. | Image/dependency work is often cached, so model and test time dominate. |

On Apple Silicon, an x86_64 test image can require linux/amd64 emulation. That can make startup and CPU-heavy tests slower than native arm64 work even when everything is correct.

### Failure modes by failure domain

| Failure domain | Symptoms | Likely cause | Correct response |
| --- | --- | --- | --- |
| Model credentials/quota | 401, 403, 429, insufficient quota. | Missing key, billing limit, rate limit. | Fix credentials/billing or back off. Retrying an exhausted quota without a change is not useful. |
| Model service | 5xx, timeout, cancelled request, no tool call, empty compaction summary. | Provider incident, network issue, incompatible model behavior, transient failure. | Bounded retry for safe requests; record step context. The code retries empty compaction summaries a limited number of times. |
| Prompt/context | Slow later turns, context-window errors, poorer decisions. | Long trajectory, huge command output, too many instructions. | Bound output, compact history, avoid repeated inspection, load only relevant skills. |
| Harness policy | Phase-gate error, inspection budget exhausted, submission incomplete. | Model asked for an action invalid in current state. | This is control feedback, not an outage. Read next_action and choose a valid action. |
| Harness implementation | Bad classification, schema mismatch, wrong state transition. | Defect in agent code. | Reproduce with mocked actions/offline tests; inspect state observations and trajectory; fix the harness. |
| Docker/Colima lifecycle | Daemon unavailable, image pull failure, platform manifest error, startup timeout. | Colima stopped, architecture mismatch, registry/network issue, low disk. | Repair Colima, use compatible platform/image, inspect disk/resources, retry fresh. |
| Modal lifecycle/control plane | Sandbox start failure, tunnel/runtime unreachable, deployment or cleanup timeout. | Scheduling, network/tunnel issue, runtime boot failure, shutdown race. | Inspect Modal logs; retry fresh sandbox when appropriate; preserve trajectory/patch before cleanup. |
| Sandbox command | Test timeout, non-zero exit, OOM kill, process-limit failure, disk full. | Broken code, flaky/slow test, low CPU/RAM/disk, invalid command. | Read stderr/exit code, run focused reproduction, tune limits only with evidence. |
| Repository/patch integrity | Empty patch, dirty baseline, unrelated patch files, test passes but wrong diff submitted. | Wrong source image, untracked changes, weak submission handling. | Verify initial revision, Git status/diff, bind test to diff digest, evaluate saved patch independently. |
| Cleanup/artifacts | Run appears to succeed but ends in cleanup error; patch missing. | Sandbox termination timeout after useful work. | Treat cleanup separately from task result; write durable artifacts before fragile cleanup where possible. |

Two distinctions matter:

1. A **phase-gate tool error** means the sandbox command never ran. It is expected feedback, not a failed test.
2. A passing test is not trustworthy unless it ran against the intended revision and the final patch was preserved. This motivates Git checks and the capability-mode diff digest.

### Reliability principles for an agent loop

#### 1. Put a budget and timeout around every expensive boundary

Model steps, sandbox startup, command execution, total sandbox lifetime, and cleanup need separate limits. A five-minute model queue, a five-minute test, and a five-minute image pull have different diagnoses and fixes.

#### 2. Retry only operations that are safe to repeat

Retrying a read-only model request or failed context compaction is usually safe. Retrying an edit or any non-idempotent action can be unsafe until Git state is checked. Git status and diff are therefore correctness mechanisms, not mere logging.

#### 3. Separate control-plane success from task success

The host process, model request, sandbox lifecycle, test command, and patch artifact can succeed or fail independently. “Modal cleanup timed out” does not prove the patch is wrong. “The model answered” does not prove a test passed. Record each result independently.

#### 4. Prefer focused verification

After a small edit, a focused test/reproduction usually gives faster feedback, lower resource use, and a clearer causal link than a large suite. Run broader verification when budget permits.

#### 5. Build observability into the trajectory

For each step, production-quality telemetry should include phase, step number, model name, prompt/token usage, model-request latency, tool name, sandbox-command latency, return code, exception/timeout, Git status, diff digest, and patch size. This project already records prompts/responses/compactions and exposes Git state; structured timings and correlation IDs would be the next production upgrade.

### Practical latency diagnosis

| What you observe | Most likely bottleneck | First thing to inspect |
| --- | --- | --- |
| Long pause before first model action | Image build/pull or sandbox startup. | Docker/Modal logs, cache, platform compatibility. |
| Long pause after “requesting action” | Model service/inference. | Prompt size, model selection, API status/quota, local Ollama resource use. |
| Long pause after a test tool call | Sandbox workload. | Exact command, output, CPU/RAM limits, filesystem/dependency work. |
| Later turns gradually slow down | Prompt growth/prefill. | Trajectory size, repeated output, compaction threshold. |
| Fast run but no usable patch | Submission or cleanup path. | Phase state, submitted patch, patch-output path, cleanup errors. |

### The trajectory JSON: flight recorder, not the whole observability stack

Your intuition is right: the trajectory JSON is the agent's most important
local record of what it **saw**, **decided**, and **asked to do**. It is very
close to a flight recorder for one run. It is invaluable for debugging model
behavior, evaluating the agent, and comparing two runs.

In this project, `Agent.run()` writes it in a `finally` block, so it is usually
saved even if the run later fails. Its top-level fields are:

| Field | What it contains | Why it is useful |
| --- | --- | --- |
| `prompts` | A snapshot of every prompt sent to the model. Each includes the system prompt, task, and prior conversation available at that point. | Shows what evidence and instructions the model actually had before making a decision. |
| `responses` | Raw normalized model responses, including tool calls. | Shows what the model chose to do, plus provider-reported usage when available. |
| `compactions` | Compaction prompts/responses and before/after context evidence. | Explains why older conversation became a summary rather than remaining verbatim. |

Because each later prompt contains prior conversation history, most earlier tool
observations are visible inside later `prompts`. For example, a test command's
stdout, stderr, Git-status evidence, and agent-state block are normally in the
next prompt snapshot. This is why you can reconstruct much of a trajectory from
the JSON.

But “most” is not “everything.” The trajectory is **not** full observability:

| Missing or incomplete information | Why it matters |
| --- | --- |
| The final tool observation may be absent. | The agent appends it after the final model response, but no next model prompt is created to snapshot that conversation turn. The final patch file is therefore authoritative for submission output. |
| Exact timings and timestamps. | The file does not directly report model latency, sandbox-command duration, queue time, startup time, or cleanup time. |
| Full raw command output in every case. | `format_tool_output()` intentionally truncates very large observations before they reach model context, so the trajectory can contain a bounded version rather than every byte. |
| Sandbox internals. | Container CPU/RAM/disk use, Modal logs, Docker/Colima daemon logs, process trees, and network diagnostics live outside this JSON. |
| A direct event ledger. | It saves API prompts/responses, not a separate canonical list of every state transition, command start/end, retry, and artifact write. Some facts are inferred from prompt snapshots. |
| Sensitive-data policy and retention metadata. | Production traces need redaction, access control, retention rules, and sometimes payload sampling. A local artifact has none of those systems by default. |

The right mental model is:

~~~text
Trajectory JSON = “What did the agent know and decide?”
Sandbox logs    = “What actually happened in the execution environment?”
Metrics/traces  = “How long did each component take, and where did it fail?”
Patch artifact  = “What durable code change is being submitted?”
~~~

All four are useful. None fully replaces the others.

### What production systems usually do

Production systems commonly keep the same *idea* as this trajectory, but make
it more structured, durable, queryable, and privacy-aware. They usually have
three complementary telemetry layers:

| Layer | Typical data | Question answered |
| --- | --- | --- |
| Agent semantic trace | User request, model input/output, tool calls, tool results, state transitions, evaluations, final answer. | Why did the agent take this action? |
| Distributed trace and metrics | Request ID, spans, timestamps, latency, error rate, token/cost metrics, queueing, CPU/RAM. | Where was time spent and which service failed? |
| Runtime/security logs | Sandbox/container logs, access decisions, policy denials, audit events. | What happened in the infrastructure, and was it permitted? |

A production event might carry a shared `run_id` and `trace_id` through the
host process, model call, sandbox command, evaluator, and artifact store. That
makes one slow user request queryable across all systems instead of requiring a
human to infer timing from a JSON file.

Production teams also normally:

- emit a structured event when a step starts and ends, with timestamps and
  status;
- record tool inputs/outputs with size limits and redact secrets or personal
  data before storage;
- store large stdout, patches, and attachments in a controlled blob store while
  keeping links/hashes in the trace;
- use metrics and alerts for error rate, latency, tool failures, cost, and
  sandbox saturation;
- retain enough trace detail for debugging, but sample, delete, or restrict
  sensitive data according to policy;
- run offline evaluations against traces to detect regressions after changing
  prompts, models, tools, or policies.

### Where LangGraph fits, and where it does not

You do **not** need LangGraph to record trajectories or build production
observability. Our custom `CodeAgent` already has explicit state and can emit
structured events. The choice is architectural, not a requirement.

LangGraph is primarily a workflow/state-machine framework. It helps declare
nodes, conditional edges, state, persistence, and recovery behavior. LangSmith
is the related tracing/observability product: its documentation shows tracing
LangGraph applications, and it can also trace custom functions or SDK calls
when they are wrapped/decorated. [LangGraph/LangSmith tracing documentation](https://docs.langchain.com/langsmith/trace-with-langgraph)

So the common options are:

| Approach | Agent orchestration | Observability approach | Good fit |
| --- | --- | --- | --- |
| Custom loop, like this assignment | Your own Python state machine. | Write local trajectories; add OpenTelemetry/logs/metrics or a tracing vendor. | Maximum control, small system, teaching, special policies. |
| LangGraph plus LangSmith | Graph nodes/edges/checkpoints. | Framework-integrated semantic traces, optionally tracing custom tools too. | Teams wanting graph semantics and an integrated tracing UI. |
| Another agent framework or internal platform | Varies. | Internal event pipeline plus standard telemetry tools. | Larger organisations with established reliability/security platforms. |

The framework does not magically create observability. Even with LangGraph,
you must decide what to trace, how to redact it, which metrics to alert on, how
long to retain data, and how to correlate sandbox logs with model/tool events.
Conversely, a well-instrumented custom loop can be more observable than a
poorly configured framework.

### Practical recommendation for this assignment

Keep using the trajectory JSON as the primary artifact for agent-quality review:
inspect its tool sequence, prompt growth, state observations, compactions, and
token usage. Pair it with the saved patch and evaluation result to decide
whether the run actually solved the task.

If this became a production service, the next upgrade would not be a mandatory
framework migration. It would be an explicit event schema, timestamps around
each model and sandbox call, a run/trace ID, secret redaction, and export to the
organisation's logging and metrics platform. LangGraph plus LangSmith would be
one reasonable implementation choice, not the definition of production.

### Principal-engineer summary

~~~text
LLM       = expensive probabilistic decision service; often accelerator-bound
Harness   = cheap deterministic control plane; mostly CPU-bound
Sandbox   = isolated execution plane; often CPU/RAM/disk-bound
Modal/Docker transport = lifecycle/control plane; often I/O-bound
Git + artifacts = durability/correctness plane
~~~

The engineering objective is not simply to make one component fast. It is to keep the critical path short, make every boundary observable, bound retries, preserve evidence, and stop an unreliable model action from becoming an unverified final result.


## 7. The state machine

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

## 8. What state is tracked?

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

## 9. State shown to the model

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

## 10. Capability mode versus legacy mode

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

## 11. How the main CodeAgent methods participate in control flow

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

## 12. The dispatcher: where everything meets

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

## 13. A concrete successful run

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

## 14. What happens when the model goes off track?

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

## 15. Best source reading order

When revising, read the source in this order:

1. CodeAgentPhase and ExecuteKind: vocabulary for state and action kinds.
2. __init__: initial state, tools, and standing policy.
3. _transition, _next_action, _agent_state: visible state machine.
4. _admit_command and _record_command_result: legacy-mode gates and transitions.
5. _handle_inspect, _handle_apply_patch, _handle_run_tests, _handle_submit: strict capability workflow.
6. execute_tool_calls: dispatcher that ties all parts together.
7. Agent.run in base.py: outer loop that repeats until finished is true or the step limit is reached.

## 16. Quick revision answers

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
