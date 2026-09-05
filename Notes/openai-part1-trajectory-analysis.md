# OpenAI Part 1 trajectory analysis

## Scope

This report compares two GPT-5-mini Part 1 runs:

- `artifacts/part1-old-trajectory.json`: historical baseline from the older CodeAgent implementation.
- `artifacts/part1-trajectory.json`: newer run after the CodeAgent phase gates, Git-state evidence, step warnings, and submission controls were added.

Both runs targeted the same `chess-terminal-move` bug: the chess API returned HTTP 500 when White made a legal move that immediately ended the game. The correct behavior is to return the final game state with `game_over: true`, the final result, and no engine reply.

The final patch currently recorded at `artifacts/openai-fix.patch` changes only `src/chess_app/game.py`:

```diff
engine_move: str | None = None
if self._board.outcome(claim_draw=True) is None:
    reply = self._choose_engine_move()
    engine_move = reply.uci()
    self._record_and_push(reply)
```

That is the right fix shape: after the human move is pushed, the game may already be terminal, so the engine should only be asked to move when the board still has an outcome of `None`.

## Executive assessment

The new trajectory is a clear improvement over the old one.

| Metric | Old run | New run | Change |
|---|---:|---:|---:|
| Model responses / action steps | 28 | 13 | 53.6% fewer |
| Prompt tokens | 337,494 | 71,920 | 78.7% fewer |
| Completion tokens | 2,747 | 1,166 | 57.6% fewer |
| Total tokens | 340,241 | 73,086 | 78.5% fewer |
| Final prompt size | 19,180 | 9,337 | 51.3% smaller |
| Compactions | 0 | 0 | unchanged |
| Premature `send_message` | yes | no | fixed |
| Git commit/reset attempts | yes | no actual mutation attempted | fixed operationally |
| Patch created from empty working-tree diff | yes | no | fixed |
| Submission path | detoured through committed diff | direct uncommitted diff | improved |

The core result is not just "fewer steps." The new run removed the most expensive and risky part of the old trajectory: the submission recovery loop caused by committing the fix, producing an empty `patch.txt`, then reconstructing the patch from `HEAD~1..HEAD`.

The old run still solved the bug, but it did so with poor operational discipline. The new run solved the same bug with a much cleaner lifecycle: inspect, reproduce, edit, test, invoke submission skill, create patch, review patch, submit.

## Old run behavior

The old trajectory had good diagnostic instincts but weak execution controls.

It correctly inspected `game.py`, `server.py`, and the test suite. It reproduced the failing terminal move through `TestClient` and observed the HTTP 500. It localized the root cause to `ChessGame.play_human_move()`, where the code always called `_choose_engine_move()` after the human move even if that move ended the game.

The source fix was correct. The failure was in the workflow around it:

- It tried to read a nonexistent `tests/test_server.py`.
- It tried to feed an `apply_patch`-style patch block into `git apply`, which Git rejected.
- It used a Python string replacement to make the edit.
- It ran `git add` and `git commit`, which was not appropriate for the assignment's patch workflow.
- It masked a failed commit with `|| true`, making the command look successful.
- It submitted before invoking the required `submit-task` skill and was rejected.
- It then ran `git reset --soft HEAD~1`, recommitted, created an empty `patch.txt` from `git diff`, diagnosed that mistake, and finally generated the patch from `git diff HEAD~1..HEAD`.

That explains the 28 responses and 340,241 tokens. The model had already found and verified the correct one-file fix by step 18; most of the remaining cost came from avoidable Git and submission-state confusion.

## New run behavior

The new trajectory is much tighter.

Its action sequence was:

1. List repository files.
2. Run the baseline test suite.
3. Reproduce the bug with the supplied terminal move and observe HTTP 500.
4. Inspect `src` and `server.py`.
5. Inspect `game.py`.
6. Reinspect the relevant `play_human_move()` region.
7. Attempt `git apply` with an invalid patch block.
8. Apply the same source change with a Python file edit.
9. Run the full test suite and observe `9 passed`.
10. Invoke `submit-task`.
11. Create `patch.txt` from `git diff -- src/chess_app/game.py` and print it.
12. Review `patch.txt` again.
13. Submit with `send_message`.

The final result is credible because the trajectory contains all required evidence:

- The bug was reproduced before the edit.
- The source edit is visible in the Git diff against the initial revision.
- The full test suite passed after the edit.
- The submission skill was invoked before `send_message`.
- The patch was created from the uncommitted working tree, not from a commit.
- The patch was reviewed in a separate step before submission.

There was still a Modal cleanup timeout outside the agent loop in one run path, which prevented the CLI from writing `artifacts/openai-fix.patch` automatically. That was an environment cleanup issue, not a failure of the trajectory itself. The patch was recoverable from the trajectory and has been reconstructed in `artifacts/openai-fix.patch`.

## What improved and why

The improvement came from harness and agent controls, not from the model suddenly becoming better at chess logic.

The standing prompt now tells the model to keep forward progress, respect phases, avoid repeated inspections, stay under 30 steps, and never stage or commit. This directly addresses the old run's two biggest sources of waste: broad wandering and Git misuse.

The observations now append `git status --porcelain` and a compact `git diff` summary against the initial revision after each accepted execute call. That gives the model objective state: whether the worktree is clean, whether a real source diff exists, how large it is, and whether the patch has changed. In the old run, the model had to ask Git manually and then made bad decisions anyway.

The phase-gated state machine makes progress visible. The model sees whether it is in discovery, implementation, verification, or submission, along with the next expected action. This reduces the chance that it keeps inspecting after the edit location is known or submits before the required skill workflow.

The Git mutation guard prevents the exact old failure mode. The old trajectory's `git add`, `git commit`, `git reset`, and history-based patch generation should never be part of this assignment path. Keeping the fix as an uncommitted working-tree diff lets `git diff -- src/chess_app/game.py > patch.txt` work directly.

Output bounding and de-duplication also helped. The old trajectory repeatedly carried duplicated command output through `output`, `stdout`, and `stderr`. The new observations are still informative, but much less bloated.

## Remaining weaknesses in the new run

The new run is better, but not perfect.

The model still tried to use `git apply` with an `*** Begin Patch` block. That is a predictable tool-protocol confusion: `*** Begin Patch` is for the harness-side patch tool, not for shell `git apply`. Since the in-sandbox agent only has shell execution, the prompt or tool surface should steer it toward one supported edit mechanism.

The new run did not rerun the exact HTTP reproduction after the edit. It ran the full test suite, which passed, and the patch is logically correct, but the strongest verification would include the specific `f7g7` request returning HTTP 200 with `engine_move: None`. The old run actually did this better.

It still spent one repeated inspection on `game.py` lines 64-120 after already opening the full file. That is small compared with the old run, but the phase gate could be stricter once the exact function has been read.

The final summary says "added guard" and "ran tests", which is correct, but it does not explicitly mention the reproduced terminal endpoint behavior after the edit because that focused post-fix reproduction was not run.

## Recommended next improvements

The highest-value improvement is to split editing from shell execution completely. The agent already has explicit capability concepts (`inspect`, `apply_patch`, `run_tests`, `submit`). Make the preferred path use `apply_patch` for edits instead of asking the model to synthesize shell file writes. Then reject `git apply` attempts that contain `*** Begin Patch` with a targeted error:

```text
This patch format is for the apply_patch tool, not git apply. Use apply_patch
or provide a valid unified diff.
```

The second improvement is verification gating. Before entering `SUBMIT`, require either:

- a focused reproduction/test command whose output includes the original failing condition now succeeding, or
- a full test command plus a newly added/modified regression test covering the reported failure.

For this assignment, the best pattern is: reproduce 500 before edit, apply fix, reproduce 200 after edit, then run `pytest -q`.

The third improvement is to add "targeted reproduction" to the next-action hint after the first implementation. The model should not have to infer that the exact bug reproduction is more valuable than broad tests.

The fourth improvement is to avoid repeated repository listing. After one `ls -la`, the harness can mark equivalent repeat commands as low-value inspection unless the worktree changed or a new directory is being explored.

The fifth improvement is to protect the patch artifact against environment cleanup failures. The CLI should write the trajectory-derived patch or preserve `patch.txt` before leaving the environment context, or it should handle Modal stop timeouts without masking a completed run. The new trajectory was good, but the outer cleanup failure caused avoidable manual patch recovery.

The sixth improvement is to make the submission summary validator stricter. It should reject unsupported claims such as "added tests" when the Git diff contains no test file. The old run made that claim during a rejected premature submission; a validator can catch that class of unsupported claim cheaply.

## Bottom line

The new Part 1 trajectory improved substantially. It reduced total tokens from 340,241 to 73,086, cut action steps from 28 to 13, avoided the old Git-history detour, followed the submission skill flow, and produced a clean uncommitted patch.

The remaining work is not about solving this chess bug. The model can already do that. The next gains are in tool design and harness enforcement: force edits through an explicit patch capability, require focused post-fix reproduction before submission, reject invalid patch formats early, and make CLI artifact writing resilient to Modal cleanup timeouts.
