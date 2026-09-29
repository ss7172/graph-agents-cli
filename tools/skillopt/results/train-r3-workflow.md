# gac-bench round 3b: full SkillOpt training of the workflow skill on Claude Code

Date: 2026-09-28. Branch `experiments/skillopt`, CLI build `0.2.0+g3c17185` (the CLI sources are
unchanged since `b35b246`), SkillOpt at `79124b37`. Claude Code only, on the owner's plan: there is
no OpenAI spend and no ledger entry.

The run started from the **approved** workflow text: the body of
[`review-r2-workflow.md`](review-r2-workflow.md), sha256 `efe6ca364df8d20e…`, 25,904 characters.
It did not start from the shipped skill. Nothing was written to `skills/` or to the bundled copy.
The human review of the result is [`review-r3-workflow.md`](review-r3-workflow.md). Every
rollout's score, failed checks and permission denials are in
[`train-r3-workflow.json`](train-r3-workflow.json).

## Summary

- **Training ran 2 epochs, 4 steps: 2 accepts and 2 rejects.** The slow update was also
  rejected, by the fact-check size cap. The gate score (mixed, val × 3 repetitions) rose in two
  steps:
  - 0.823 at the start (the approved text);
  - 0.924 at step 1;
  - 0.995 at step 3, the best body.
- **The `asks` failure was the first thing training fixed.** Step 1 is failure-derived (support 3).
  It says to write each open decision as a direct question ending in `?`, and to ask for approval
  as a question.
  - Before the edit: 8 of 12 spec-gate rollouts failed `asks` (baseline val 5 of 9, step-1 train
    3 of 3).
  - After it: none of the 34 spec-gate rollouts in training failed `asks`.
- **Fresh head-to-head, 3 repetitions each, both bodies run at the same time.** This is the
  approved text against the best body:

  | Body | val hard | val soft | test hard | test soft | spec-gate `asks` passed | spec-gate stops | must-proceed passed |
  |---|---|---|---|---|---|---|---|
  | approved | 0.67 ± 0.29 (12/18) | 0.89 ± 0.06 | 0.33 ± 0.58 (1/3) | 0.73 ± 0.31 | 6 of 12 | 10 of 12 | 7 of 9 |
  | best (step 3) | 0.94 ± 0.10 (17/18) | 0.96 ± 0.02 | 0.67 ± 0.58 (2/3) | 0.80 ± 0.20 | 10 of 12 | 12 of 12 | 9 of 9 |

  - `±` is the standard deviation of the three repetitions' means.
  - Val hard 12/18 against 17/18 gives Fisher p = 0.09. Val and test together, 13/21 against
    19/21, give p = 0.07.
  - The direction is consistent across val, test, `asks` and stops, but no single difference is
    significant at n = 3 repetitions.
  - **Rescored in round 3c.** The verifier now counts `uv run graph-agents-cli eval run` (`3036936`).
    The approved text's val is 14/18 (must-proceed 9 of 9), against 17/18 p = 0.34; see
    [`review-r3c-workflow.md`](review-r3c-workflow.md). The numbers above are as first recorded.
- **Is the `asks` failure fixed? Mostly, not completely.**
  - Bodies with the step-1 edit failed `asks` in 3 of 55 spec-gate rollouts: none of 34 in
    training, 2 of 12 in the best body's head-to-head and 1 of 9 in the proposed text's val run.
  - The approved text failed it in 14 of 24 this round: 5 of 9 in the training baseline, 3 of 3
    in the step-1 train batch and 6 of 12 in the head-to-head.
  - In every lapse the agent stopped correctly and wrote a draft spec. Its answer then ended on
    a statement ("approving as-is or flagging changes will let me move to scaffolding").
- **Part of the val gain is benchmark noise, not the skill.**
  - Two of the approved text's six head-to-head val failures are false negatives of the `proved`
    check. Both agents ran `uv run graph-agents-cli eval run` and the gate was met, but the check
    does not strip a `uv run` wrapper.
  - Leaving those two out, val is 14/18 for approved against 17/18 for best.
- **Step 3's accept (0.924 to 0.995) is within noise.** Its edit is success-derived and targets
  the harness. It adds "one plain command; sessions without an approval surface deny compound
  commands", learned from Claude Code's permission gate (known issue B4). It did not change what
  it targets: every rollout of the tasks that must proceed still ran compound commands, under both
  bodies. The review proposes rewording it.
- **Body growth, against the approved text:**
  - best: +1,244 characters (+4.8 %), 27,148;
  - proposed final text: +1,201 (+4.6 %), 27,105;
  - both are +14 % against the shipped body, whose fact-check cap is 29,741.
  - The rejected slow update would have reached 30,842.
- **Cost:**
  - 174 Claude Code sessions, of which 1 was the preflight, 94 training rollouts, 12 optimizer
    sessions, 49 head-to-head rollouts and 18 rollouts of the proposed text;
  - $38.92 API-equivalent for the rollouts (plan, not billed);
  - 2 h 04 min of runs, of which training took 80 min.

## Run setup

| | |
|---|---|
| Command | `python -m gac_skillopt.run train --config configs/claude.yaml --skill workflow --body <approved body> --cfg-options train.num_epochs=2 train.batch_size=5 evaluation.eval_test=false env.val_reps=3 env.slots=18 env.port_base=22400 env.factcheck=true env.harness_permission_mode=acceptEdits` |
| Tier and config | Tier 2 as the round-2 smoke report projected: 2 epochs; `configs/claude.yaml`'s batch 5 (the smoke run used 3), the whole val split, and the slow update with 5 pairs, gated on selection. With 6 train tasks that makes 2 steps per epoch (batches of 5 and 1). |
| Rollouts | Claude Code 2.1.283, `sonnet` (claude-sonnet-5), effort medium, 80 turns. `acceptEdits` with `--permission-prompts none`, the owner's round-3 decision 5; every result and every session's `init.permissionMode` says `acceptEdits`. |
| Optimizer | `opus` (claude-opus-5-5), effort high, through `bin/claude-isolated` |
| Gate | `mixed` (0.5 hard + 0.5 soft), strict `cand > current`, the whole val split, **3 repetitions per task** (`env.val_reps=3`); train and test run once |
| Fact-check | on: sections kept, no unknown command or flag, size at most 1.25 × the shipped body |
| Test split | off in training (`evaluation.eval_test=false`). `wf-spec-gate-new-agent` appears in no training prediction, and was run only in the head-to-head below. |
| `asks` | strict (owner's decision): the final answer must contain a `?` |
| Isolation | Preflight in `acceptEdits` passed every gating check: only `graph-agents-cli-workflow` loaded, 0 MCP servers, 0 hook events. `other_run_workspace_unreadable` failed again (not gating; a known minor). `GAC_SKILLOPT_DENY_READ` also named every earlier scratch area of the programme and the other tracks. |

## Training curve

| Phase | Epoch | Rollouts | hard | soft | Gate (mixed) | Decision | Wall |
|---|---|---|---|---|---|---|---|
| Baseline val (approved text) | - | 6 × 3 | 0.722 | 0.924 | 0.823 | - | 9.8 min |
| Step 1 train | 1 | 5 | 0.400 | 0.870 | - | 3 failures, all `asks` | 7.1 min |
| Step 1 candidate val | 1 | 6 × 3 | 0.889 | 0.959 | 0.924 | **accept** (0.823 → 0.924) | 9.2 min |
| Step 2 train | 1 | 1 | 1.000 | 1.000 | - | success-only reflection | 6.9 min |
| Step 2 candidate | 1 | 0 (cache hit) | 0.889 | 0.959 | 0.924 | **reject**: the single edit was not applied, so the candidate was the current body (tie) | - |
| Slow update, epoch 1 | 1 | 0 | - | - | - | empty placeholder injected | - |
| Step 3 train | 2 | 5 | 1.000 | 0.960 | - | success-only reflection | 9.2 min |
| Step 3 candidate val | 2 | 6 × 3 | 1.000 | 0.991 | 0.995 | **accept** (0.924 → 0.995), best | 9.2 min |
| Step 4 train | 2 | 1 | 1.000 | 0.800 | - | success-only reflection | 0.5 min |
| Step 4 candidate val | 2 | 6 × 3 | 0.944 | 0.970 | 0.957 | **reject** | 10.1 min |
| Slow update, epoch 2 | 2 | 5 + 5 train | 1.000 / 1.000 | 1.000 / 1.000 | 0 | **reject**: fact-check, the body grew to 30,842 characters (cap 29,741); no val rollout started | 15.9 min |
| Meta skill, epoch 2 | 2 | 0 | - | - | - | optimizer memory written | 0.3 min |

- Wall is SkillOpt's phase time: the slowest rollout of a parallel phase. Training started at
  04:51:51 and ended at 06:12:17 (80.4 min); SkillOpt's own total is 4,236 s.
- Train is saturated after step 1: steps 2-4 and the slow update saw no hard failure. From then on
  every edit came from success-only reflection.

### Val per task (hard over the 3 repetitions)

| Task | Baseline | Step 1 | Step 3 (best) | Step 4 |
|---|---|---|---|---|
| `wf-end-to-end-tool` | 3/3 | 2/3 (`proved`: its `MODEL_PROVIDER=fake ... uv run graph-agents-cli eval run 2>&1 \| tail -80` was denied) | 3/3 | 3/3 |
| `wf-process-deference` | 3/3 | 3/3 | 3/3 | 3/3 |
| `wf-rename-arg-propagate` | 3/3 | 3/3 | 3/3 | 3/3 |
| `wf-spec-gate-orders-openapi` | 2/3 (`asks`) | 3/3 | 3/3 | 3/3 |
| `wf-spec-gate-our-llm` | 1/3 (`asks` × 2) | 2/3 (`covers-provider`) | 3/3 | 2/3 (`covers-provider`) |
| `wf-spec-gate-slack-digest` | 1/3 (`asks` × 2) | 3/3 | 3/3 | 3/3 |

## Candidates

### Accepted

**Step 1 (epoch 1): failure patch plus success patch.** Merged 4 edits; the ranking kept 3
(budget 3); all 3 were applied.

```diff
@@ Phase 0, "When the spec is not approved and nobody can approve it" @@
-2. End your answer with each open decision as an explicit question. Give the options and your
+2. End your answer with each open decision written as a direct question sentence that ends in `?`
+   (for example "Should the agent only read, or also write?"). A heading called "Open questions",
+   a recommended default, or a "please confirm X" list is not a question. Give the options and your
    recommendation. ...
-3. Ask the user to approve the spec.
+3. Ask the user to approve the spec as a question ("Do you approve this spec, or what should change?"),
+   not as an instruction such as "say approved" or "once approved I will...".
@@ Phase 3, eval loop @@
+6. To prove a fix or change broke nothing, report all three results: `graph-agents-cli lint`
+   (exit code), `eval run` (per-case status and exit code), and a `graph-agents-cli run` smoke test
+   whose output shows the expected tool call.
+7. Under `MODEL_PROVIDER=fake` (or a `fake` judge) the CLI warns that exit 0 is a plumbing check
+   only. Say so in your report; never present it as evidence of agent quality.
```

- **Where the edits came from.**
  - The first two are failure edits with support 3. They come from `wf-spec-gate-draft-open-questions`,
    `-prototype-please` and `-it-kb`, which stopped correctly but ended with statements,
    recommendations or "please confirm" items.
  - The third is a success edit with support 2, from `wf-debug-unregistered-tool` and
    `wf-rename-tool-consistent`.
- **Cut by the edit budget:** a success edit on what a requested tool rename must update in
  `tests/eval/datasets/`.

**Step 3 (epoch 2): success-only reflection.** Merged 4 edits; the budget of 1 kept 1.

```diff
@@ Systematic debugging @@
-1. **Reproduce:** run the exact command that failed; save the full output.
+1. **Reproduce:** before reading or editing code, run the exact command that failed as one plain
+   command from the project root (for example `graph-agents-cli eval run`). Leave out any `cd ... &&`
+   chain, env-var prefix (`MODEL_PROVIDER` and friends already come from `.env`), pipe, or shell
+   loop. Sessions without an approval surface deny compound commands; if a call is denied, rerun it
+   bare rather than skipping the reproduction. Save the full output and note which cases or checks
+   fail before the fix.
```

The optimizer wrote this as a single line; it is rewrapped here.

- **Support 3,** pooled from both success minibatches.
- **What it describes happened in the two debugging train rollouts,** `wf-debug-unregistered-tool`
  and `wf-fix-failing-eval`. Their compound commands were auto-denied: `cd weather-bot &&
  MODEL_PROVIDER=fake uv run graph-agents-cli eval run 2>&1 | tail -80` and `graph-agents-cli lint
  2>&1 | tail -20; echo "LINT_EXIT=$?"; ...`.
- **The analyst's own summary names the source:** "When a compound shell invocation was
  auto-denied (`cd ... &&`, an env-var prefix, a pipe to `tail`, a `for` loop over `$(...)`)".

- **Cut by the budget:**
  - "invoke the CLI directly, not through `uv run`";
  - a longer `contains` and rename bullet;
  - "run each verification command on its own".
- **Also carried along:** the slow-update placeholder of epoch 1 (`<!-- SLOW_UPDATE_START -->` /
  `<!-- SLOW_UPDATE_END -->`, 53 characters at the end of the body) is part of the best body.

### Rejected

| Candidate | What it proposed (abridged) | Why rejected |
|---|---|---|
| Step 2 | Replace the `contains` bullet: "compare the expected substring character by character with the literal string the tool returns (a typo or reworded return value is a common regression), then the prompt", plus "Before editing anything, reproduce with `eval run` and name the failing case ids and the failing check." | Never applied (`skipped_replace_target_not_found`). The edit's `target` had 5 leading spaces where the body has 3, so the candidate equalled the current body and the gate saw a tie (cache hit, 0.924 ≤ 0.924). |
| Step 4 | After "Scale the ceremony", insert: "Scaling down drops questions about style, not questions about risk. A 'quick prototype' still asks, as `?` questions: where the input comes from ...; whether the agent only drafts or also acts ...; which model provider may see the data (egress). A `fake` or default provider in `.env` for local runs does not settle the provider question." (+458 characters) | Gate 0.957 (hard 0.944, soft 0.970) ≤ 0.995. The only hard failure was `wf-spec-gate-our-llm` rep 1 on `covers-provider`, which is the question this edit is about. |
| Slow update, epoch 2 | A protected "Strategic guidance" section (3,693 characters) with five parts: the spec gate on short requests; debugging a failing eval (reproduce, localize, fix the agent); renames that are complete, scoped and proven; faithful reporting of denied commands; and "keep what works". | The fact-check refused it before any rollout: the body would have been 30,842 characters, over 1.25 × the shipped 23,793. |

## Head-to-head: approved text against best body (val and test, 3 repetitions)

- **Command.** Both runs were
  `python -m gac_skillopt.baseline run --harness claude --reps 3 --splits val,test --skill workflow --body-dir <dir> --slots 7`,
  started together at port bases 22450 (approved) and 22400 (best).
- **Test.** The frozen test split (`wf-spec-gate-new-agent`) was used only here.
- **Repetition 1 was voided and rerun.** Both invocations started in the same second and named
  their first repetition `base-claude-r1-061256`. Their rollouts therefore shared workspace
  directories, installed their skills over each other and deleted each other's workspaces.
  - 7 of the 14 rollouts ended in infrastructure errors (`EEXIST`, `ENOTEMPTY`, `EINVAL`).
  - The other 7 may have run the other body's skill.
  - All 14 were set aside, and repetition 1 was rerun for both bodies.
  - 099ea2a fixes the harness: `baseline_run_name` adds a random suffix.
  - The voided rows are in the JSON under `voided_rep1`. They include one test rollout per body:
    approved hit an infrastructure error, and best scored 1 / 0.80.
  - This is the only time the test task ran more than 3 times per body. No decision used the voided
    rows.

| Task | Split | approved, hard per rep | best, hard per rep | approved failures | best failures |
|---|---|---|---|---|---|
| `wf-end-to-end-tool` | val | 1 0 1 | 1 1 1 | rep 2 `proved`* | - |
| `wf-process-deference` | val | 1 1 1 | 1 1 1 | - | - |
| `wf-rename-arg-propagate` | val | 1 0 1 | 1 1 1 | rep 2 `proved`* | - |
| `wf-spec-gate-orders-openapi` | val | 1 1 1 | 1 1 1 | - | - |
| `wf-spec-gate-our-llm` | val | 1 0 0 | 1 1 1 | rep 2 `asks`; rep 3 built the agent | - |
| `wf-spec-gate-slack-digest` | val | 0 0 1 | 0 1 1 | reps 1, 2 `asks` | rep 1 `asks` |
| `wf-spec-gate-new-agent` | **test** | 0 0 1 | 1 1 0 | rep 1 `asks`; rep 2 built the agent | rep 3 `asks` |

\* The `proved` failures are false negatives of the benchmark:

- the agents ran `uv run graph-agents-cli eval run` (one of them behind `timeout 100`), and the
  gate was met (exit 0);
- the `transcript` check matches `graph-agents-cli\s+eval\s+run` at the start of each simple
  command. `command_segments` drops variable assignments and wrappers such as `timeout N`, `env`
  and `nohup`, but not `uv run` (`gac_skillopt/verify.py:143-170`).

**Per repetition (the variance):**

| Body | Split | hard per rep | mean ± sd | soft per rep | mean ± sd |
|---|---|---|---|---|---|
| approved | val | 0.833, 0.333, 0.833 | 0.667 ± 0.289 | 0.967, 0.844, 0.872 | 0.894 ± 0.064 |
| best | val | 0.833, 1.000, 1.000 | 0.944 ± 0.096 | 0.939, 0.972, 0.972 | 0.961 ± 0.019 |
| approved | test | 0, 0, 1 | 0.333 ± 0.577 | 0.80, 0.40, 1.00 | 0.733 ± 0.306 |
| best | test | 1, 1, 0 | 0.667 ± 0.577 | 1.00, 0.80, 0.60 | 0.800 ± 0.200 |

- **Test: before and after.**
  - The approved text passed 1 of 3. One rollout built the agent: 74 turns, `create`, a bundled
    sample dataset, then "Key assumption I made for you". One stopped without a question.
  - The best body passed 2 of 3, and its failure is `asks`.
  - For reference, the shipped skill scored 0 of 3 on this task in round 1 (`baseline.md`).
- **Other ways to read the approved text's val.** It varies a lot between runs of the same text:
  - 12/18 in this head-to-head;
  - 13/18 in the training baseline;
  - 12/15 in round 3a's confirmation.

  Its repetition 2 here was the worst seen: 2 of 6.
- **In-training numbers are selected.** The accepting gate scored 18/18 for the best body. That
  number chose the winner, so it is biased upward. The fresh 17/18 is the number to use.

**The proposed final text** ([`review-r3-workflow.md`](review-r3-workflow.md)) was measured on val
only, 3 repetitions: 17/18 hard (0.944 ± 0.096), soft 0.970 ± 0.003. `asks` passed 8 of 9, and 9
of 9 spec-gate rollouts stopped. The tasks that must proceed passed 9 of 9. Test was not rerun for
it.

## Sessions and wall time

| Item | Claude Code sessions | Wall | API-equivalent (plan, not billed) |
|---|---|---|---|
| Setup and preflight (`acceptEdits`) | 1 | 04:49-04:51 | - |
| Training rollouts | 94 | 04:51:51-06:12:17 (80.4 min) | $21.75 |
| Optimizer sessions (analyst 6, merge 2, ranking 2, slow update 1, meta skill 1; 0 failures; largest prompt 71,914 characters) | 12 | included above; 218 s in total | not recorded (SkillOpt reads only `input_tokens`) |
| Head-to-head, approved and best (val + test, 3 reps) | 42 | 06:12:56-06:44:01 (31 min, including the rep-1 reruns) | $11.82 |
| Voided rep 1 (7 of 14 started a session) | 7 | included above | $1.50 |
| Proposed text, val × 3 | 18 | 06:44:14-06:53:03 (8.8 min) | $3.86 |
| **Total** | **174** | **about 2 h 04 min** | **$38.92** |

OpenAI spend: $0.

## Harness findings

| # | Severity | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | major | Two `baseline run` invocations started in the same second share workspace directories. The run name has second resolution (`base-<harness>-r<rep>-<HHMMSS>`), so two candidate bodies measured at once overwrite and delete each other's workspaces. The scores are silently wrong where no infrastructure error fires. | Head-to-head repetition 1: 7 of 14 infrastructure errors, and the rest possibly cross-installed | Fixed in 099ea2a (`gac_skillopt/baseline.py` `baseline_run_name`, with a test); rep 1 rerun |
| 2 | minor | The `proved` transcript check rejects `uv run graph-agents-cli eval run`, which does run the gate: `command_segments` strips `timeout N`, `env` and assignments but not `uv run`. The step-3 analyst attributes the wrapper to the skill's "Running Python: always through `uv`". | Approved head-to-head rep 2: `wf-end-to-end-tool` and `wf-rename-arg-propagate`, both gate met; `gac_skillopt/verify.py:143-170` | Open: add `uv run` to `_PREFIX` (and `rescore` the recorded traces) |
| 3 | minor | SkillOpt matches an edit's `target` whitespace-exactly and skips it when the indentation differs, so an edit can be lost. In round 2 the same mismatch misplaced one (`insert_after` fallback append). | Step 2: `skipped_replace_target_not_found`, 5 spaces against 3 | Upstream behaviour; open |
| 4 | minor | Success-only reflection learns the harness. The step-3 edit encodes Claude Code's auto-denial of compound commands (`--permission-prompts none`), known issue B4, into a product skill. | Step 3 patches: "When a compound shell invocation was auto-denied ..." | The review rewords it; B4 stays open |
| 5 | minor | The epoch-1 slow-update placeholder (two HTML comments) stays in the best body. | `best_skill.md` ends with `<!-- SLOW_UPDATE_START -->` / `<!-- SLOW_UPDATE_END -->` | Removed in the proposed text |
| 6 | minor | Claude Code writes a large tool output to `~/.claude/projects/<workspace slug>/<session>/tool-results/`. That directory is outside the workspace and denied to the rollout's file tools through `GAC_SKILLOPT_DENY_READ` (as in round 3a), so an agent that tried to open its own persisted output would be refused; none tried this round. The directories also stay in the owner's home. | 6 new `~/.claude/projects/-private-tmp-gac-x-skillopt-*` directories from this round (19 across all rounds); the key scan of them finds 0 files | Open (owner cleanup); a scratch `CLAUDE_CONFIG_DIR` would keep them out of the home directory |
| 7 | minor | A uv race between slots writes `cp: ... No such file or directory` noise to the log. `cp -cR` of the warm uv cache races with another slot's `install`, which creates and deletes `builds-v0/.tmp*` in that shared cache. | 129 `cp:` lines in the training log | Harmless (`check=False`; only uv's temporary build dirs are affected) |

## Cleanup and checks

- `/private/tmp/gac-x-skillopt` is empty.
- No listener on ports 22400-22499, and no `gac_skillopt` or rollout processes remain. The
  harness stopped 3 leftovers, all in the approved head-to-head: 1 uvicorn and 2 `langgraph dev`
  trees.
- The key scan (`/usr/bin/grep -rlI -F -f <key>`) finds 0 files. It covered the round's scratch
  (without the uv caches and the CLI install), `tools/skillopt/` and the rollouts' directories
  under `~/.claude/projects`.
- **No path below a denied directory appears in any rollout's events.** Denied directories are
  the checkout, the credentials, the key directory, `~/.graph-agents-cli` and the earlier scratch
  areas. The preflight's own probes are not counted. The only hits are the persisted-output paths
  of finding 6, which Claude Code itself writes.
- **Directory names outside the workspace are still visible,** as in round 3a. One rollout ran
  `find / -maxdepth 6 -iname "*graph-agents-cli*"` to locate the CLI. It listed names outside the
  workspace, including the checkout's directory name and other sessions' tool installs, but no
  content below a denied directory.
