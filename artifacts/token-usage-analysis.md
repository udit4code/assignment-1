# GPT-5-mini context-compaction experiment

## Executive summary

This experiment compares two runs of the `django__django-15368` SWE-bench task
using the same resolved model deployment, `gpt-5-mini-2025-08-07`:

- **Compacted run:** context compaction enabled at an estimated 6,000 active
  tokens.
- **Baseline run:** context compaction disabled with `COMPACT_THRESHOLD=0`.

Both runs completed the agent workflow, ran the focused existing Django test
module successfully, invoked the submission skill, and produced a non-empty
patch. The compacted run generated the general duck-typed fix suggested by the
issue, while the baseline generated the narrower explicit `F` fix. Both are
plausible solutions to the reported defect.

The compacted run used **61,679 tokens for normal action calls**, 15,711 fewer
than the baseline's 77,390 action-call tokens. That is a **20.3% reduction** if
compaction calls are excluded. However, the two summaries themselves consumed
12,474 tokens. Including that overhead, the compacted run consumed **74,153
tokens**, only 3,237 fewer than the baseline, for a net reduction of **4.2%**.

Compaction therefore achieved its main context-management goal: it reduced the
largest action prompt from 9,968 tokens to 5,856 tokens, a **41.3% reduction in
peak action-prompt size**. In this short task, though, a late second compaction
had little remaining context to amortize its cost over. Compaction also broke
the stable prompt prefix and received fewer cached input tokens, so lower raw
token usage does not automatically imply lower API cost.

## Inputs and methodology

The comparison uses these recorded artifacts:

| Condition | Trajectory | Patch |
|---|---|---|
| Compacted | `artifacts/django__django-15368-gpt-5-mini-trajectory.json` | `artifacts/django__django-15368-gpt-5-mini.patch` |
| No compaction | `artifacts/django__django-15368-gpt-5-mini-baseline-trajectory.json` | `artifacts/django__django-15368-gpt-5-mini-baseline.patch` |

Token counts come directly from each recorded API response's `usage` object.
Normal agent calls are stored in `.responses`; compaction calls are stored
separately in `.compactions[].compaction_response`. The inclusive compacted
total adds both sets exactly once.

Two accounting details are important:

1. `cached_tokens` is already included in `prompt_tokens`; it must not be added
   to the total a second time.
2. `reasoning_tokens` is already included in `completion_tokens`; it is useful
   diagnostically but is not an additional category to add to total tokens.

The runs used the same task, model deployment, default service tier, agent
prompt, tools, and verification command. They were separate stochastic model
runs, not deterministic replays. Consequently, the observed difference
includes both the effect of compaction and differences in the actions selected
by the model.

## Aggregate token results

| Metric | Compacted | Baseline | Difference |
|---|---:|---:|---:|
| Normal action calls | 14 | 12 | +2 |
| Compaction calls | 2 | 0 | +2 |
| Total model calls | 16 | 12 | +4 (+33.3%) |
| Action prompt tokens | 60,640 | 76,682 | -16,042 |
| Action completion tokens | 1,039 | 708 | +331 |
| **Action-call total** | **61,679** | **77,390** | **-15,711 (-20.3%)** |
| Compaction prompt tokens | 10,764 | 0 | +10,764 |
| Compaction completion tokens | 1,710 | 0 | +1,710 |
| **Compaction-call total** | **12,474** | **0** | **+12,474** |
| **Inclusive prompt tokens** | **71,404** | **76,682** | **-5,278 (-6.9%)** |
| **Inclusive completion tokens** | **2,749** | **708** | **+2,041** |
| **Inclusive total tokens** | **74,153** | **77,390** | **-3,237 (-4.2%)** |
| Cached prompt tokens | 34,176 | 57,088 | -22,912 |
| Non-cached prompt tokens | 37,228 | 19,594 | +17,634 (+90.0%) |
| Reasoning tokens | 384 | 64 | +320 |
| Largest action prompt | 5,856 | 9,968 | -4,112 (-41.3%) |

The action-only number makes compaction look substantially better because it
does not charge the two summarization requests. The inclusive number is the
appropriate measure of total model traffic: compaction saved 16,042 action
prompt tokens but spent 12,474 tokens constructing summaries, leaving a modest
net saving.

The cache result changes the cost interpretation. Approximately 47.9% of the
compacted run's inclusive prompt tokens were cached, compared with 74.4% for
the baseline. The baseline repeatedly extended a stable prefix, which is well
suited to prompt caching. Each compaction replaced the old conversation with a
new summary, and the compaction prompts themselves recorded no cached tokens.
As a result, the compacted run processed 90.0% more non-cached prompt tokens.
Actual monetary cost depends on the provider's cached-input, uncached-input,
and output-token rates, so this report does not equate the 4.2% raw-token saving
with a 4.2% cost saving.

## Compaction events

| Event | Agent step | Estimated tokens before | Estimated tokens after | Immediate reduction | Compaction-call tokens |
|---|---:|---:|---:|---:|---:|
| 1 | 7 | 6,276 | 2,414 | 3,862 (61.5%) | 6,523 |
| 2 | 13 | 6,310 | 2,187 | 4,123 (65.3%) | 5,951 |

Both events materially reduced active context, satisfying the functional goal
of compaction. The first event occurred around the transition from diagnosis
and failed editing attempts to successful implementation and verification. It
was useful because several model turns still remained.

The second event occurred after the fix had passed the focused tests and the
agent was already in the submission workflow. Only one additional successful
action was ultimately needed after that summary. The estimator reported 6,310
tokens before and 2,187 after compaction. Using those estimates and the actual
final action size, the 5,951-token summary request was unlikely to recover its
own cost over a single remaining turn. This is the clearest inefficiency in the
compacted run and suggests suppressing routine compaction once the agent is in
`SUBMIT`, unless the context is close to the model's hard limit.

## Prompt growth by action step

| Step | Compacted prompt | Baseline prompt |
|---:|---:|---:|
| 1 | 1,372 | 1,382 |
| 2 | 3,085 | 2,429 |
| 3 | 4,387 | 4,142 |
| 4 | 4,650 | 5,444 |
| 5 | 5,036 | 5,862 |
| 6 | 5,605 | 6,431 |
| 7 | 5,856 | 6,731 |
| 8 | 2,750 | 7,087 |
| 9 | 4,289 | 8,623 |
| 10 | 4,728 | 9,062 |
| 11 | 5,247 | 9,521 |
| 12 | 5,434 | 9,968 |
| 13 | 5,644 | — |
| 14 | 2,557 | — |

The compacted trajectory shows the intended sawtooth pattern. Prompt size grew
through step 7, dropped sharply after the first compaction, grew again through
step 13, and dropped again after the second compaction. The baseline prompt
grew monotonically from 1,382 to 9,968 tokens.

This is the strongest argument for compaction on genuinely long tasks: without
it, each new step repeatedly resends an ever-larger history. Even when the net
token saving is small on a short run, bounding the active prompt reduces the
risk of exceeding a context window and keeps old, noisy observations from
dominating the model's attention.

## Behavioral comparison

### Compacted run

The compacted agent used 14 action steps:

1. It inspected a broad section of `django/db/models/query.py` and then the
   relevant `bulk_update()` section.
2. It performed an unnecessary branch/status inspection.
3. Its first edit attempt used `git apply` with an incompatible
   `*** Begin Patch` payload and failed with `error: unrecognized input`.
4. After additional inspection, it successfully changed the type check to
   `hasattr(attr, "resolve_expression")`.
5. It ran `queries.test_bulk_update`; all 29 tests present in the base image
   passed.
6. It invoked `submit-task` and created `patch.txt`.
7. It attempted an unrelated source inspection during `SUBMIT`, then attempted
   to submit before separately reviewing `patch.txt`. Both calls were correctly
   rejected by the phase gates.
8. It recovered, reviewed `patch.txt`, and successfully submitted.

The resulting patch is 990 bytes. It makes the general duck-typing change
described in the issue and adds four explanatory comment lines.

### No-compaction baseline

The baseline used 12 action steps:

1. It began with a broad directory listing and two source inspections.
2. Its first `git apply` edit attempt also failed.
3. It inspected the target and confirmed the replacement string before editing.
4. It successfully changed the condition to
   `isinstance(attr, (Expression, F))`.
5. It ran the same focused module; all 29 tests present in the base image
   passed.
6. It invoked `submit-task`, created and separately reviewed `patch.txt`, and
   submitted without the compacted run's two rejected submission actions.

The baseline patch is 671 bytes. It implements the narrower alternative
explicitly permitted by the issue statement and changes only one source line.

### What this means for the comparison

The baseline happened to follow a shorter path and produced a smaller patch.
The compacted run's extra two action calls were caused by submission mistakes,
not by compaction alone. Conversely, compaction preserved enough factual state
for the agent to recover from those mistakes and finish. Because action paths
differed, this single pair of runs demonstrates observed behavior rather than a
statistically isolated causal effect. A stronger experiment would replay
multiple runs per condition with the same model, limits, prompt, and sandbox,
then compare medians and failure rates.

## Correctness and evaluation status

Both trajectories establish the following:

- A source patch was created and submitted.
- The patch changed only `django/db/models/query.py`.
- The focused base-image module ran 29 tests and reported `OK` in each run.
- The changes remained uncommitted, so the generated patch contained the fix.

Those 29 tests are the task's existing PASS_TO_PASS coverage. The decisive
`test_f_expression` FAIL_TO_PASS test is supplied separately by
`tasks/swebench/django__django-15368/test_patch.diff`; it was not present during
the agent's own test command. The repository contains no saved evaluator report
showing `FAIL_TO_PASS (1/1)`, `PASS_TO_PASS (29/29)`, and `RESOLVED: yes`.
Therefore, the evidence supports **agent workflow success and likely patch
correctness**, but this report does not claim a formally recorded SWE-bench
resolution.

The compacted patch can be formally graded without another model call using:

```bash
SWEBENCH_PATCH=artifacts/django__django-15368-gpt-5-mini.patch \
make check-swebench INSTANCE=django__django-15368
```

The baseline patch can be graded with:

```bash
SWEBENCH_PATCH=artifacts/django__django-15368-gpt-5-mini-baseline.patch \
make check-swebench INSTANCE=django__django-15368
```

## Tradeoffs and recommendations

### Benefits of compaction

- It bounded peak action-prompt size by about 41%, leaving more headroom for a
  longer task or larger tool output.
- It prevented monotonic prompt growth and materially reduced active context at
  both compaction points.
- It reduced action-call token traffic by 20.3% and inclusive model traffic by
  4.2% in this observed run.
- Its summaries retained the objective, edit, test result, failure history,
  repository state, and next action well enough for the agent to complete.

### Costs and risks of compaction

- Summary generation added two requests and 12,474 tokens.
- The run made 16 total model calls instead of 12, increasing request latency
  and opportunities for transient API failure.
- Replacing history with summaries reduced prompt-cache reuse and nearly
  doubled non-cached prompt tokens.
- A generated summary can omit or distort evidence. This run's summaries were
  useful, but the mechanism introduces an additional model-dependent failure
  surface.
- The second summary occurred too late to amortize its cost.
- The comparison is noisy because the two stochastic runs selected different
  actions and fixes.

### Recommended policy

Keep compaction for long-running SWE-bench work, but make it phase- and
horizon-aware:

1. Compact only when the active prompt crosses the threshold and enough action
   steps remain to amortize the summary request.
2. Skip ordinary compaction in `SUBMIT`; submission should usually require only
   skill invocation, patch creation/review, and final messaging.
3. Retain the deterministic agent state and latest complete tool interaction
   verbatim, as the current implementation does.
4. Track action tokens and compaction tokens separately, while reporting an
   inclusive total as the primary efficiency metric.
5. Track cached and non-cached prompt tokens separately when discussing cost.
6. Use several runs per condition before concluding that compaction improves
   completion rate, cost, or step efficiency.

## Conclusion

The compacted run met the assignment's context-management requirement: it
triggered compaction twice and materially reduced active context. It also
completed the coding workflow and produced a likely-correct patch. Relative to
the no-compaction baseline, it reduced total recorded model tokens by 4.2% and
peak action-prompt size by 41.3%, but required four additional model requests
when compaction calls are included and received substantially less cache reuse.

For this short task, compaction's net token advantage was modest. Its larger
value is controlling context growth and preserving reliability on longer tasks.
The experiment also shows that a late submission-phase compaction should be
avoided because it adds summary cost when too few turns remain to repay it.
