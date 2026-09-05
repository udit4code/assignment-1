# Analysis of the historical OpenAI Part 1 trajectory

## Scope and provenance

This report analyzes:

- `artifacts/openai-part1-trajectory.json`
- `artifacts/openai-fix.patch`

The trajectory was generated with `gpt-5-mini-2025-08-07`. It contains 28
model action responses and no context-compaction events. The patch and
trajectory are byte-for-byte identical to the later copies named
`artifacts/fix.patch` and `artifacts/part1-trajectory.json`; those filenames do
not represent independent runs.

Most importantly, this is a **historical baseline produced by the old
CodeAgent implementation**. The run predates most of the advanced controls
added later, including phase-gated execution, inspection quotas, automatic Git
status and baseline-diff evidence, forbidden Git mutation checks, explicit
coding capabilities, bounded/deduplicated observations, repeated-action
warnings, and stronger submission-state enforcement. Its behavior should be
judged in that historical context rather than as evidence of how the current
CodeAgent behaves.

## Executive assessment

The run was **functionally successful but operationally inefficient and not
fully rule-compliant**.

It successfully:

- Identified the real defect in `ChessGame.play_human_move()`.
- Reproduced the original HTTP 500 response for a checkmating human move.
- Implemented a correct source change.
- Re-ran the same reproduction and observed HTTP 200, `game_over: True`,
  `status: White wins`, and `engine_move: None`.
- Ran the existing nine-test suite successfully after the edit.
- Created a non-empty, valid unified diff containing only the intended source
  file.
- Finished with `send_message` and produced `openai-fix.patch`.

However, it also:

- Spent 28 model actions and 340,241 recorded tokens on a small one-file fix.
- Performed several broad or redundant inspections.
- Tried to read a nonexistent test file.
- Used an invalid `git apply` payload.
- Staged and committed the solution despite the submission workflow requiring
  uncommitted changes.
- Masked a failed commit with `|| true`, producing a misleading zero exit code.
- Tried to submit before invoking the required submission skill.
- Recommitted after attempting to undo the first commit.
- Initially generated an empty `patch.txt` because normal `git diff` cannot see
  an already committed change.
- Needed multiple extra Git-history commands and repeated patch reviews to
  recover.

The final patch is credible and directly supported by reproduction evidence,
but the trajectory itself demonstrates why the later CodeAgent safeguards were
necessary.

## Task and root cause

The task reported that the chess HTTP API returned status 500 when White made a
legal move that immediately ended the game. The supplied example set this FEN:

```text
7k/5Q2/6K1/8/8/8/8/8 w - - 0 1
```

and played `f7g7`, which checkmates Black.

The old implementation of `play_human_move()` always requested an engine move
after recording White's move:

```python
reply = self._choose_engine_move()
engine_move = reply.uci()
self._record_and_push(reply)
```

After `f7g7`, the board was already terminal and had no legal Black move.
Calling `_choose_engine_move()` in that state raised an exception, which
propagated through the HTTP layer as a server error.

The agent correctly localized this control-flow bug. Its solution checks the
board outcome after the human move and requests an engine reply only while the
game remains active:

```python
if self._board.outcome(claim_draw=True) is None:
    reply = self._choose_engine_move()
    engine_move = reply.uci()
    self._record_and_push(reply)
```

This preserves the existing non-terminal behavior while allowing checkmate,
stalemate, repetition, and other claimable terminal outcomes to return their
final state without manufacturing a Black response.

## Trajectory walkthrough

### 1. Initial repository discovery: steps 1–7

The agent began with `ls -la`, then read `src/chess_app/game.py` and
`src/chess_app/server.py`. Those inspections were relevant and were enough to
identify the likely control-flow location.

It then attempted to read `tests/test_server.py`, which did not exist. The
command returned exit code 2. The agent recovered by listing `tests/`, finding
the correct filenames, and reading both `tests/test_chess_server.py` and
`tests/test_chess_game.py`.

This recovery was reasonable, but the sequence illustrates an avoidable cost:
a bounded file listing or targeted search before opening a guessed path would
have eliminated the failed action.

### 2. Baseline verification and reproduction: steps 8–10

At step 8, the agent ran the full existing suite:

```text
9 passed, 1 warning
```

This established that the starting repository passed its regression tests, but
the public FAIL_TO_PASS test had not yet been injected into the sandbox.

At step 9, the agent wrote a focused reproduction with `TestClient`, configured
the supplied FEN, and posted `f7g7` to `/api/move`. It observed:

```text
500
```

The Python command itself returned exit code 0 because the snippet printed the
HTTP result instead of asserting it. The agent nevertheless interpreted the
printed 500 correctly. This is valid human-readable evidence, although an
assertion-based reproduction would have produced stronger machine-readable
pass/fail semantics.

Step 10 checked Git status and the branch name. This was not needed to determine
the fix and would later be supplied automatically by the improved harness.

### 3. Editing: steps 11–15

The first edit attempt passed an `*** Begin Patch` block to `git apply`. That is
not a standard Git unified diff, so the command failed with:

```text
error: No valid patches in input
```

The agent then reread a broad numbered view of the source file and used a Python
string replacement. The replacement succeeded and printed `patched`.

The source edit was correct. The operational mistake came immediately after
it: the agent ran `git add -A && git commit ... || true`. Git rejected the
commit because no author identity was configured, but `|| true` converted the
overall shell result to exit code 0. This is a dangerous pattern in an agentic
workflow because it makes a failed state-changing command look successful to a
harness that relies only on the process exit code.

At step 15, the agent configured a repository-local Git identity, staged the
change, and committed it successfully. Committing was unnecessary and directly
conflicted with the later submission instruction to keep the source change
uncommitted.

### 4. Post-fix verification: steps 16–18

The verification sequence was strong:

- Step 16 ran the focused normal-move regression test and observed `1 passed`.
- Step 17 reran the exact terminal-move reproduction and observed status 200.
- The returned JSON reported `game_over: True`, `status: White wins`, and
  `engine_move: None`.
- Step 18 ran the complete existing suite and again observed `9 passed`.

Together, these checks demonstrate both sides of the behavioral contract:
terminal human moves no longer trigger an engine reply, while ordinary moves
continue to work. This is substantially better evidence than relying only on a
green preexisting suite.

The agent did not edit or submit a regression test. That was correct for this
assignment's patch-submission rules because the evaluator injects the public
test from `tasks/chess-terminal-move/public_tests/test_patch.diff` separately.

### 5. Submission problems and recovery: steps 19–28

At step 19, the agent tried to call `send_message` before loading the mandatory
`submit-task` skill. The harness correctly rejected the request. The attempted
summary also claimed that the agent had “added tests,” although no test file was
changed. This was an unsupported statement. The later final summary did not
repeat that claim.

At step 20, the agent invoked `submit-task`. The skill explicitly instructed it
to keep changes uncommitted, create `patch.txt` from `git diff`, review that
patch in a separate step, and only then submit.

Instead of following that instruction directly, step 21 ran:

```bash
git reset --soft HEAD~1 ... && git add -A && git commit ...
```

The soft reset briefly moved the change back into the index, but the same
command immediately recommitted it. Consequently, step 22's normal working-tree
diff produced an empty `patch.txt`. Step 23 confirmed that the file contained
zero bytes.

The agent eventually diagnosed the issue and inspected the committed change
with `git diff HEAD~1..HEAD`. At step 25 it created `patch.txt` from that commit
range instead of from the working tree. Steps 26 and 27 reviewed the same patch
again, and step 28 finally submitted successfully.

This recovery preserved the solution, but it consumed eight actions after the
first submission attempt. More importantly, it depended on repository history
that should never have been modified in the first place.

## Patch assessment

`openai-fix.patch` is a 988-byte unified diff affecting only:

```text
src/chess_app/game.py
```

Its headers use the expected `--- a/` and `+++ b/` paths. It does not include
tests, configuration, generated files, binary data, or unrelated edits.

The code change is narrowly scoped and consistent with the task. The four-line
comment is somewhat verbose for a straightforward guard, but it accurately
describes the failure and does not change behavior.

The recorded evidence strongly supports correctness:

| Check | Before fix | After fix |
|---|---:|---:|
| Existing test suite | 9 passed | 9 passed |
| Normal engine-reply test | Not separately run | 1 passed |
| Checkmating `f7g7` HTTP status | 500 | 200 |
| Final state says game over | Request failed | True |
| Final status | Request failed | White wins |
| Engine reply after checkmate | Request failed | None |

The repository does not contain a persisted `check-part1` evaluation report,
so this analysis does not claim formal replay success in a fresh evaluator
sandbox. It concludes that the patch is likely correct based on the recorded
before/after reproduction and regression suite.

## Token and context analysis

The trajectory recorded the following usage:

| Metric | Value |
|---|---:|
| Action/model calls | 28 |
| Prompt tokens | 337,494 |
| Completion tokens | 2,747 |
| Total tokens | 340,241 |
| Cached prompt tokens | 297,472 |
| Non-cached prompt tokens | 40,022 |
| Reasoning tokens | 1,024 |
| Average prompt tokens per action | 12,053 |
| Average total tokens per action | 12,151 |
| First action prompt | 1,091 |
| Final action prompt | 19,180 |
| Compaction events | 0 |

Prompt size grew by 18,089 tokens from the first to the final action, reaching
17.6 times its initial size. Because the trajectory never compacted, every
later request carried the accumulated history, including broad file dumps,
failed commands, repeated patch content, and duplicate terminal streams.

Approximately 88.1% of prompt tokens were recorded as cached, which likely
reduced the monetary effect of repeatedly sending the stable prefix. Cached
tokens are still part of context length and total-token accounting, however.
The high cache rate therefore does not solve the attention, latency, or context
window risks of an ever-growing prompt.

The old output formatter also included identical information in `output`,
`stdout`, and `stderr` fields when the streams overlapped. For example, test
results and commit messages appeared more than once in the same observation.
The newer formatter removes those duplicates and bounds observation size,
directly addressing one source of the 340,241-token total.

## What the old agent did well

Despite the inefficiency, several behaviors were solid:

- It grounded its diagnosis in source and test inspection.
- It established a baseline before editing.
- It created a focused reproduction that demonstrated the reported 500.
- It recovered from missing files and an invalid patch format.
- It made a minimal source-only change.
- It verified the exact bug after editing, not merely unrelated tests.
- It preserved normal move behavior with a focused regression test.
- It ran the full existing suite after the change.
- It obeyed the harness rejection requiring the submission skill and eventually
  produced a valid patch.
- All 28 model responses used tool calls; it did not spend action turns on
  ungrounded prose-only reasoning.

These strengths show that the model understood the engineering task. The main
problems were orchestration and state management rather than root-cause
reasoning.

## What the later CodeAgent implementation improves

The current CodeAgent adds controls specifically targeted at the failure modes
visible here.

### Phase-gated state machine

The current agent exposes `DISCOVER`, `IMPLEMENT`, `VERIFY`, and `SUBMIT` phases
with a deterministic `next_action`. This prevents continued general inspection
after the root cause is known and makes the remaining workflow explicit. The
old trajectory had only a generic instruction to inspect, fix, verify, and send
a message, so the model had to infer all lifecycle state itself.

### Inspection budgets

Discovery and later-phase inspection quotas prevent unlimited browsing. The
historical run spent multiple calls on a missing test path, directory listing,
broad file rereads, branch inspection, and repeated patch reads. The newer
agent rejects further inspection after the budget is exhausted and directs the
model toward editing or focused verification.

### Objective Git progress evidence

The current legacy interface captures the initial Git revision and internally
runs both a baseline diff and `git status --porcelain` after each accepted
command. It appends compact evidence to the observation and uses it for phase
transitions. The old run had to request Git state explicitly and could easily
misinterpret whether a command created a durable change.

### Git mutation protection

The current agent rejects model-issued `git add`, `commit`, `reset`, `restore`,
`checkout`, `switch`, `stash`, `clean`, `merge`, and related history/index
mutations before they reach the sandbox. The standing prompt and compaction
prompt repeat the same constraint.

Had that protection existed during this run, steps 14, 15, and 21 would have
been rejected. The source change would have remained in the working tree,
`git diff -- src/chess_app/game.py > patch.txt` would have worked immediately,
and most of steps 21–27 would have been unnecessary.

### Submission enforcement

The newer state machine does not permit submission actions before verified
source progress. It also tracks whether `patch.txt` was created and reviewed in
separate steps. The older harness did successfully reject the premature
`send_message`, but it did not stop the agent from mutating Git history or
creating an empty patch first.

### Explicit capabilities

The later optional capability interface separates `inspect`, `apply_patch`,
`run_tests`, and `submit`. This lets the harness validate arguments and enforce
phase transitions without guessing intent from arbitrary shell text. The old
trajectory exposed a generic `execute` tool, which allowed invalid patch syntax,
shell masking with `|| true`, commits, resets, and ad hoc submission recovery.

### Output and repetition controls

The current implementation bounds observations, removes duplicated
stdout/stderr when already represented by combined output, warns on repeated
identical execution calls, and can compact older context into factual working
memory. None of those controls materially shaped this historical run.

## Counterfactual efficient trajectory

With the current safeguards, the same task should require roughly this sequence:

1. Inspect `game.py`, the relevant server route, and existing tests.
2. Run an assertion-based reproduction confirming the terminal move currently
   returns 500.
3. Apply the outcome guard without staging or committing.
4. Run the exact reproduction and focused regression tests.
5. Invoke `submit-task`.
6. Create `patch.txt` from the uncommitted source diff.
7. Review `patch.txt` once.
8. Submit the summary.

That is not a requirement that every stochastic run use exactly eight calls.
It illustrates that the historical 28-call path contained substantial
avoidable overhead, especially after the correct edit was already verified.

## Final verdict

`openai-part1-trajectory.json` is valuable as a baseline showing that
GPT-5-mini could solve the underlying bug even with a minimally controlled
agent. Its diagnosis, implementation, and behavioral verification were sound,
and `openai-fix.patch` is narrowly scoped and likely correct.

It should **not** be presented as a trajectory generated by the current
advanced CodeAgent. It predates the later phase gates and safeguards, and its
Git history mutations violate the intended uncommitted-patch workflow. The
run's excessive context growth, repeated observations, submission detour, and
340,241-token cost provide concrete motivation for the later implementation.

For evaluation or documentation, the fairest label is:

> Historical GPT-5-mini Part 1 baseline generated by the old CodeAgent before
> phase-gated execution, objective Git progress detection, explicit capability
> tools, context controls, and Git mutation safeguards were added.
