# gac-bench round 2: 1-epoch SkillOpt smoke train on Claude Code (workflow, scaffold)

Date: 2026-09-27. Branch `experiments/skillopt`; CLI build `0.2.0+gb35b246`; SkillOpt at
`79124b37`. Round 2's A7 step: `python -m gac_skillopt.run train` end to end, 1 epoch, Claude Code
only (the owner's plan; no OpenAI spend). Raw traces, events, candidates and step records stay in
the run's scratch directory. Nothing was written to `skills/` or to the bundled copy.

## Summary

- **The whole trainer path ran for both skills.** It went rollouts, reflection, aggregation,
  ranking, candidate, fact-check, val gate, then accept or reject. There were 5 steps, 4 accepts
  and 1 reject. The fact-check reject path was exercised separately (see *Fact-check gate*).
- **Both targeted edits appeared in the first epoch.** The optimizer wrote them from train and val
  failures, and neither skill was hand-edited:
  - *workflow:* a rule for sessions in which nobody can approve the spec. Stop before `create`,
    leave a draft spec and ask the open decisions.
  - *scaffold:* pass `--agent-guidance-filename CLAUDE.md` or `GEMINI.md` only when the user names
    that agent. The agent running `create` is not the team's choice.
- **Scores before and after, on val.** "Before" is the shipped skill: 3 repetitions, step 6's two
  plus this run's baseline. "After" is the best skill: 2 confirmation repetitions plus the
  accepting gate run.

  | Skill | Before hard / soft | After hard / soft | Targeted check, before → after |
  |---|---|---|---|
  | workflow | 0.67 / 0.73 (n=18) | 0.78 / 0.96 (n=18) | stops before `create` in spec-gate tasks: 3/9 → 9/9 |
  | scaffold | 0.71 / 0.89 (n=21) | 1.00 / 1.00 (n=21) | guidance file kept when no agent is named: 6/12 → 12/12 |

- **Why workflow hard only reaches 0.78.** Every remaining workflow failure is one check, `asks`:
  the final answer must contain a question. The candidate stops correctly, but 4 of 9 spec-gate
  rollouts leave the questions in the draft spec and end with "the questions are listed in the
  spec file", with no `?`. The rule the optimizer wrote says to end the answer with explicit
  questions, so this is a lapse in following the rule, not a benchmark fault. The `\?` regex is a
  thin proxy, though (see *Go/no-go*).
- **Not exercised in 1 epoch:**
  - slow update: epoch 1 only injects an empty placeholder;
  - the meta skill: skipped in epoch 1;
  - the test split: turned off with `evaluation.eval_test=false`. Test tasks stay frozen and unseen,
    and the flag also skips the final re-validation of a skill that differs only by that
    placeholder.
  - With 2-3 steps, the cosine edit budget went 2 → 1 and never reached `learning_rate: 3`.
- **Gate noise is the main finding for full runs.**
  - Scaffold's step-3 accept (0.857 → 1.00) came from a Claude permission-gate denial in step 2's
    val, not from the step-3 edit: the auto-denied `create` in `scaffold-guidance-unstated`.
  - The shipped workflow skill scored val hard 0.50 in this baseline, against 0.75 over two
    repetitions in step 6.
  - Single-rollout val of 6-7 tasks cannot tell a real edit from a flake.

## Run setup

| | |
|---|---|
| Command | `python -m gac_skillopt.run train --config configs/claude.yaml --skill <workflow\|scaffold> --cfg-options train.num_epochs=1 train.batch_size=3 evaluation.eval_test=false env.slots=<6\|7> env.port_base=<22450\|22460>` |
| Rollouts | Claude Code 2.1.283, `sonnet` (claude-sonnet-5), effort medium, 80 turns; permission mode `acceptEdits` with `--permission-prompts none`, as in round 1 and step 6. The owner's bypass-with-sandbox mode awaits its preflight. |
| Optimizer | `opus` (claude-opus-5-5), effort high, through `bin/claude-isolated`, `claude_code_exec_use_sdk: cli` |
| Gate | `mixed` (0.5 hard + 0.5 soft), strict `cand > current`, the whole val split, 1 rollout per task |
| Fact-check | on (`env.factcheck: true`) |
| Splits | workflow 6 train / 6 val; scaffold 7 train / 7 val (the round-2 splits) |

Preflight before the runs (`python -m gac_skillopt preflight`) found the following:

- only `graph-agents-cli-workflow` was listed, with 0 MCP servers and 0 hook events;
- a write outside the workspace was denied, `example.com` was blocked and PyPI returned 200;
- the checkout could not be read by the shell or by the Read and Search tools.

The optimizer wrapper got its own probe: the same flags SkillOpt uses, plus
`--output-format stream-json`. Its `init` event listed no tools, no skills and no MCP servers, and
only the built-in `agents-md` plugin. The model answered "SKILLS: NONE / MCP: NONE /
INSTRUCTIONS: NONE". It runs with cwd in a temporary directory under `/var/folders`, outside the
home tree.

## Per-step results

### workflow (45 min wall, 2 steps)

| Phase | Rollouts | hard / soft | Wall | Notes |
|---|---|---|---|---|
| Baseline val (shipped) | 6 | 0.50 / 0.69 | about 584 s | All 3 spec-gate tasks built the agent (`create` ran). |
| Step 1 train | 3 | 0.33 / 0.42 | 718 s | draft-open-questions and it-kb built the agent; rename-tool passed |
| Step 1 reflect / aggregate / select / update | - | - | 21 / 17 / 6 / 0 s | 2 analyst calls, 1 merge, 1 ranking; 4 edits merged, 2 kept (budget 2) |
| Step 1 candidate val | 6 | 0.67 / 0.93 | 464 s | **ACCEPT** (gate 0.594 → 0.800). Spec-gate rollouts now stop in about 50 s. |
| Step 2 train | 3 | 1.00 / 0.93 | 300 s | spec-gate-prototype-please passes; it failed 2 of 2 in step 6 |
| Step 2 reflect / aggregate / select / update | - | - | 25 / 0 / 4 / 0 s | success-only: 1 analyst call, 1 ranking; 3 edits → 1 (budget 1) |
| Step 2 candidate val | 6 | 0.83 / 0.97 | 564 s | **ACCEPT** (0.800 → 0.900); only slack-digest fails, on `asks` |

### scaffold (15.5 min wall, 3 steps)

| Phase | Rollouts | hard / soft | Wall | Notes |
|---|---|---|---|---|
| Baseline val (shipped) | 7 | 0.71 / 0.89 | about 276 s | guidance-unstated and default-lgs passed `CLAUDE.md` |
| Step 1 train | 3 | 1.00 / 0.93 | 48 s | no failures: success-only reflection |
| Step 1 reflect / aggregate / select / update | - | - | 20 / 0 / 5 / 0 s | 3 edits → 2 |
| Step 1 candidate val | 7 | 0.71 / 0.89 | 59 s | **REJECT** (tie: 0.800 ≤ 0.800) |
| Step 2 train | 3 | 0.67 / 0.87 | 29 s | guidance-default-prototype passed `CLAUDE.md` |
| Step 2 reflect / aggregate / select / update | - | - | 18 / 15 / 10 / 0 s | 5 edits → 2 |
| Step 2 candidate val | 7 | 0.86 / 0.91 | 292 s | **ACCEPT** (0.800 → 0.886). Both guidance tasks pass. unstated's `create` was auto-denied by the permission gate. |
| Step 3 train | 1 | 1.00 / 1.00 | 25 s | the 7th train task (the last batch has 1 item) |
| Step 3 reflect / aggregate / select / update | - | - | 18 / 0 / 5 / 0 s | 3 edits → 1 |
| Step 3 candidate val | 7 | 1.00 / 1.00 | 111 s | **ACCEPT** (0.886 → 1.000). Most likely noise, see the summary. |

Phase wall time is the slowest rollout in the phase, because all of a phase's rollouts run in
parallel. For workflow that is the rename and end-to-end tasks (420-600 s); for scaffold, it is
`scaffold-enhance-jwt-okta` (56-412 s). With the shipped skill a spec-gate rollout took
450-900 s and cost $1.1-2.7 API-equivalent. With the candidates, the 12 spec-gate val rollouts
took 31-60 s of agent time and cost $0.11-0.20.

### Optimizer sessions (`optimizer_calls.jsonl`)

| Run | Sessions | By stage | Wall per call | Largest prompt | Failures |
|---|---|---|---|---|---|
| workflow | 6 | analyst 3, merge 1, ranking 2 | 6-25 s (91 s total) | 72k characters | 0 |
| scaffold | 8 | analyst 4, merge 1, ranking 3 | 5-20 s (105 s total) | 51k characters | 0 |

Two notes on these sessions:

- **Timeout.** SkillOpt cuts each optimizer call at 300 s and retries it up to five times. No
  caller passes a timeout. `gac_skillopt/optlog.py` now defaults the timeout to 1200 s. None of
  these calls came near 300 s, so the change is a guard, not a measured saving.
- **Usage.** SkillOpt's token tracker reads only `input_tokens` from the Claude Code result event,
  so every prompt shows 2 tokens (cache reads and writes are dropped). The prompt size in
  characters is the usable proxy.

## Candidates (abridged diffs against the shipped body)

**workflow, step 1 (accepted) → step 2 (accepted, best).** Phase 0 gains:

```diff
+**What counts as approval.** Only the user explicitly approving the spec, in the conversation. None of these is approval:
+
+- a task phrased as "build it" or "get it going";
+- a spec file whose status says draft, or that still lists open questions;
+- defaults you chose yourself, even safe ones, even if you recorded them in the spec as assumptions;
+- a user who says they are unavailable, or who tells you to "do what is safe".
+
+When nobody can approve, the safe action is to stop before `create`, `scaffold enhance`, or any agent code. Do these instead:
+
+1. Write or update a draft `.graph-agents-cli-spec.md` and leave it marked unapproved.
+2. End your answer with each open decision as an explicit question. Give the options and your recommendation. Typical open decisions: which API operations and what access, which credential, who calls the agent and how they authenticate, the model provider (data egress), and the runtime, CD mode and registry.
+3. Ask the user to approve the spec.
```

The best skill has two more edits:

- **Step 1.** Systematic debugging gets: "If a test fails in code your change did not touch, show
  that it is unrelated before you move on (grep, rerun alone, report as pre-existing)."
- **Step 2.** Phase 3's eval loop gets: "When a previously passing eval breaks, fix the agent,
  never the eval." It adds two failure-to-code mappings:
  - `tool_calls` with no calls means the tool is not registered;
  - a `contains` failure means the tool's return text or the prompt.

The body grew from 23,793 to 25,561 characters (+7.4 %; the fact-check cap is +25 %).

**scaffold, step 2 (accepted) → step 3 (accepted, best).** Step 2 made the targeted edit:

```diff
-- `--agent-guidance-filename` defaults to `AGENTS.md` (read by Codex and most coding agents);
-  pass `CLAUDE.md` (Claude Code) or `GEMINI.md` (Gemini CLI, Antigravity) when that agent is in use.
+- `--agent-guidance-filename` defaults to `AGENTS.md` (read by Codex and most coding agents).
+  Pass `CLAUDE.md` (Claude Code) or `GEMINI.md` (Gemini CLI, Antigravity) only when the user says
+  the team uses that agent alone. The coding agent running `create` is not the team's choice, so
+  do not pick the file after yourself. When nobody names an agent, omit the flag and say in your
+  report that the guidance file stayed `AGENTS.md`.
```

The best skill has two more edits:

- **Step 2, `scaffold enhance`.**
  - "A value the task states is the user's answer. When the user cannot be asked, keep what
    `create_params` records."
  - A check-after list: `create_params`, `IMAGE_REPOSITORY` in `.github/agent.env`,
    `image.repository`, "Skipping (your code and config)", then `lint`.
- **Step 3.** After `create`, read `create_params` and check each stated choice. Omit `--model`
  for "default model", and run `install` or `git init` only when asked.

The body grew from 23,644 to 24,803 characters (+4.9 %).

The rejected scaffold step-1 candidate added two things:

- a note that `env.OPENAI_BASE_URL` in the chart values is a CHANGE-ME placeholder that `deploy`
  refuses outside dev;
- a "map the request's wording to flags" list.

**Prose claims checked against the source.** The fact-check gate cannot read prose, so these were
checked by hand:

- **True:**
  - the CHANGE-ME refusal outside dev (`deploy/cmd_deploy.py` `_check_chart_env`);
  - `create_params` in the manifest;
  - `IMAGE_REPOSITORY` in `.github/agent.env`;
  - "Skipping (your code and config)" (`scaffold/utils/merge.py`);
  - `create` has no base-URL flag;
  - an `app/tools` module without `TOOLS` silently contributes no tools
    (`getattr(module, "TOOLS", [])`).
- **Imprecise:** "must export a literal `TOOLS` list". The list does not have to be a literal.

**Review items before any write-back.** These are for the owner, not for this step:

- **Harness wording.** One bullet of the workflow rule ("a user who says they are unavailable,
  or who tells you to 'do what is safe'") restates the benchmark's own fixed prompt prefix
  (`tasks.PROMPT_PREFIX`). The rule is right, but that bullet should be generalized, for
  example to "a session in which nobody can answer".
- **Over-stopping.** The rule should not stop legitimate unattended work. The 3 val tasks that
  must proceed (end-to-end-tool, process-deference, rename-arg-propagate) passed in all 12 of
  their rollouts with the candidates, 4 each. The two step-2 train tasks that must proceed
  (debug-unregistered-tool, fix-failing-eval) also passed.

## Confirmation (best skills, val, 2 repetitions)

`python -m gac_skillopt.baseline run --harness claude --reps 2 --splits val --skill workflow --skill scaffold --body-dir <best bodies> --slots 12 --port-base 22450`:
26 rollouts, 17 min.

| Skill | rep 1 | rep 2 | Failures |
|---|---|---|---|
| workflow | 5/6 | 4/6 | `asks` only (orders-openapi rep 1; our-llm and slack-digest rep 2); all 6 spec-gate rollouts stopped before `create` |
| scaffold | 7/7 | 7/7 | none |

## Fact-check gate

The fact-check gate never fired in training, because every candidate passed it.

**The reject path, run directly.** `python -m gac_skillopt.run eval --skill scaffold --body <bad>`
used a body with `## Migration note` renamed and a `create --made-up-flag` example:

- it returned `hard=0 soft=0` in 1.3 s, with the reason "the '## Migration note' section is
  missing; `graph-agents-cli create` has no option --made-up-flag";
- it started no Claude session and created no workspace.

**What reaches the analysts.** SkillOpt's step buffer tells the next step only which edits were
rejected and at what score. The fact-check reason never reaches the analysts. DESIGN section 6
said it did, and is corrected.

## Harness findings

| # | Severity | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | major | The permission gate adds noise to the val gate (known issue B4). Claude's `acceptEdits` gate auto-denies commands such as `MODEL_PROVIDER=fake graph-agents-cli create ...`, `export X="$(...)"` and `cd x && ...`. | Rollouts with at least one denial: 10 of 24 (workflow), 3 of 35 (scaffold), 3 of 26 (confirmation). One flipped a val score, and that flip made scaffold's step-3 accept. | Open: the owner's bypass-with-sandbox preflight, or several rollouts per val task |
| 2 | minor | A second `result` event replaced the final answer. When an agent left a background task running, Claude Code answered again after the task's notification. `trace.parse_claude` kept only that one-line aside, so final-answer checks read the wrong text, and turns and duration were wrong (62 turns reported as 1). | 2 of 85 rollouts (wf-spec-gate-it-kb, wf-end-to-end-tool). Re-parsing all 85 streams with the fix changed only these two; hard scores are unchanged, and it-kb's `covers-*` checks flip to pass. | Fixed (ccc4daa) |
| 3 | minor | SkillOpt's 300 s optimizer timeout with five retries, and optimizer sessions missing from its counts. | See *Optimizer sessions*. | Guarded (b939f06) |
| 4 | minor | The fact-check reason does not reach the analysts. | See *Fact-check gate*. | DESIGN corrected; open |
| 5 | minor | The logged-in account's email address is in every Claude session's context. | 2 `scaffold-enhance-jwt-okta` rollouts (scaffold step-3 val, confirmation rep 1) used it as `dev-token --sub`. It appears in no candidate or patch. | DESIGN section 3.1 residual |
| 6 | minor | The workflow skill routes to sibling skills that are not installed. Rollouts call `skill graph-agents-cli-scaffold`, which fails. | Baseline spec-gate traces | DESIGN section 10 decision 2 (siblings) |
| 7 | minor | Train saturates quickly. Once the targeted failures pass, reflection is success-only and still proposes 1-2 edits per step, which grows the body. | workflow step 2 train 1.00; scaffold steps 1 and 3 | Watch for it in full runs |

## Sessions and cost

| Item | Claude sessions | API-equivalent (plan, not billed) |
|---|---|---|
| Optimizer isolation probe + rollout preflight | 2 | about $0.1 |
| workflow train (24 rollouts + 6 optimizer) | 30 | $14.37 (rollouts) |
| scaffold train (35 rollouts + 8 optimizer) | 43 | $4.17 (rollouts) |
| Confirmation (26 rollouts) | 26 | $4.85 |
| Fact-check reject check | 0 | $0 |
| **Total** | **101** | about $23.5 for the rollouts; the optimizer's cost is not recorded (item 3) |

OpenAI spend: $0, so there is no ledger entry.

## Go/no-go for full-size runs

**Must change first:**

1. **Remove the permission-gate noise.** Run the bypass-with-sandbox preflight (the owner's
   decision in ROUND2). If it passes, switch the rollout mode and rerun the train+val baseline in
   that mode. Otherwise, keep `acceptEdits` and accept the noise, knowingly.
2. **Measure val with repetitions.**
   - The problem: a single rollout per val task moves the gate on one flake (scaffold step 3), and
     the shipped workflow skill varied 0.50 against 0.75 between repetitions.
   - Proposal: add `env.val_reps` to the adapter, which runs each selection item k times and
     returns the mean. Use k = 2 at least.
   - Cost: V extra rollouts per phase, with the same wall time if there are enough slots.
3. **Decide on the `asks` check** (workflow spec-gate tasks, `final_regex: \?`). Either keep it
   strict, since the optimizer's own rule asks for explicit questions and training may learn it,
   or accept questions in the draft spec that the final answer points to. Decide this before a
   full workflow run, because it is now the only failing check on workflow val.
4. **Do not full-train scaffold on these splits.** Val is saturated at 1.00 by the best skill, so
   every later candidate can only be rejected: about 110 rollouts of pure cost. The targeted edit
   is found; what remains is human review and write-back. Harder scaffold val variants would be
   needed first.

**Projected full-size cost** (configs/claude.yaml: batch 5, whole val, slow update with 5 pairs and
gated; phase times from this run):

| Run | Rollouts | Optimizer sessions | Wall |
|---|---|---|---|
| workflow, tier 2 (2 epochs), val × 1 | about 75 | about 15 | about 2.3 h (about 15 sequential phases of about 9 min; bounded by the rename and end-to-end tasks) |
| workflow, tier 2, val × 2 | about 117 | about 15 | about 2.3 h at 12 slots |
| scaffold, tier 1 (3 epochs), val × 1 | about 126 | about 22 | about 1.5 h (21 phases of 3-5 min). Not recommended (item 4). |
| observability | - | - | No Claude signal (val 1.00 in step 6). The salt edit needs a Codex run or human review. |

Per-rollout time with the candidate rule drops by a factor of about 10 on spec-gate tasks. A
workflow run that accepts the rule early is therefore cheaper than step 6's rollout times suggest.
