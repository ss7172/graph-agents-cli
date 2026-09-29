# Human review of the round-3b SkillOpt candidate (workflow)

Date: 2026-09-28. Branch `experiments/skillopt`. This page reviews the best body of the round-3b
full training of the workflow skill ([`train-r3-workflow.md`](train-r3-workflow.md)) against the
text the owner approved in round 3a ([`review-r2-workflow.md`](review-r2-workflow.md)). It uses
the same method as [`review-r2.md`](review-r2.md).

**Nothing has been written back.** `skills/` and `src/graph_agents_cli/skills/data/` are unchanged
on this branch. The complete proposed `SKILL.md` is at the end of this page.

## What was reviewed

| Text | Body characters | Body sha256 | `SKILL.md` sha256 |
|---|---|---|---|
| Approved (round 3a; the training's initial skill) | 25,904 | `efe6ca364df8d20e848fced1594bd821523df5325347d389d0f79366cd27a248` | `e0b81aafba09d35be3f7ef870b7ef051904956e9ae6a5c6a5f807725fb95ee3e` |
| Optimizer's best (step 3) | 27,148 (+4.8 %) | `a7ec7673a405e1468ba2bacf8b7f8e09d5ca8642ad2fe9a26a37a433c269a819` | `dadf76e877ff67b62c0e65f3edfa394c997e8a48f6b25a1e33921148ed216863` |
| **Proposed final** | 27,105 (+4.6 %) | `508d1ceb8c3d59ef2938243ec33eaa99c57be7ff149633904b0bdf9b86367516` | `12d24bf9d7ac7eda381c4390138b2b2ad20b43729a4bd36f55221c3dcf64b5cb` |

- **Body and frontmatter.** The "body" is `SKILL.md` without its frontmatter (`tasks.split_skill`).
  The frontmatter is the approved file's, unchanged. The shipped body has 23,793 characters: the
  proposed text is +13.9 % against it, and the fact-check cap is +25 %.
- **Fact-check.** All three bodies pass `factcheck.check_candidate`: sections kept, no unknown
  command or flag, and within the size cap.
- **Provenance.** It comes from each step's `step_record.json`, `merged_patch.json`,
  `ranked_edits.json`, `edit_apply_report.json` and `patches/*.json` in the run's scratch.
- **Line numbers.** Source line numbers are at `099ea2a`. The CLI sources are unchanged since
  `b35b246`, the build every round-3 run used.

**How the hunks are classified** (as in round 2, plus one class):

- **targeted fix**: a failure-derived edit that addresses the open failure, here the `asks` check;
- **success-derived addition**: written by success-only reflection from trajectories that already
  passed;
- **risky**: the hunk, or part of it, has a factual error, wording copied from the benchmark or the
  harness, a misplacement, or a rule that could block legitimate work;
- **artefact**: text the optimizer's machinery inserted that is not guidance.

## Summary

| Hunk | Class | Provenance | Verdict for write-back |
|---|---|---|---|
| R1 Phase 0, step 2: each open decision as a direct question ending in `?` | **targeted fix** | step 1, failure patch, support 3 (`wf-spec-gate-draft-open-questions`, `-prototype-please` and `-it-kb` all stopped but ended without a question) | keep as is |
| R2 Phase 0, step 3: ask for approval as a question | **targeted fix** | step 1, failure patch, support 3 (same three rollouts) | keep as is |
| R3 Phase 3, steps 6-7: the three-part proof and the fake-model caveat | success-derived | step 1, success patch, support 2 (`wf-debug-unregistered-tool`, `wf-rename-tool-consistent`) | keep; "per-case status" becomes "the per-status counts" |
| R4 Systematic debugging, step 1: reproduce as one plain command | success-derived, **risky** (harness) | step 3, success-only reflection, support 3; learned from Claude Code's auto-denial of compound commands in `wf-debug-unregistered-tool` and `wf-fix-failing-eval` | keep the procedure; drop the harness sentence; rewrap |
| R5 End of file: `<!-- SLOW_UPDATE_START -->` / `<!-- SLOW_UPDATE_END -->` | **artefact** | the epoch-1 slow-update placeholder, which SkillOpt injects empty | remove |

**Measured effect.** All numbers are fresh rollouts in `acceptEdits`, 3 repetitions. "Mean ± sd" is
over the repetitions.

| Text | val hard | val soft | test hard | test soft | spec-gate `asks` passed | spec-gate stops | must-proceed passed |
|---|---|---|---|---|---|---|---|
| approved | 0.67 ± 0.29 (12/18) | 0.89 ± 0.06 | 0.33 ± 0.58 (1/3) | 0.73 ± 0.31 | 6 of 12 | 10 of 12 | 7 of 9 (2 are check false negatives) |
| optimizer's best | 0.94 ± 0.10 (17/18) | 0.96 ± 0.02 | 0.67 ± 0.58 (2/3) | 0.80 ± 0.20 | 10 of 12 | 12 of 12 | 9 of 9 |
| **proposed final** (val only) | 0.94 ± 0.10 (17/18) | 0.97 ± 0.00 | not run | not run | 8 of 9 | 9 of 9 | 9 of 9 |

- **Test.** The frozen test split has one task, `wf-spec-gate-new-agent`. It was run once for the
  approved and the best body (3 repetitions each), and not for the proposed text.
- **Significance.** No difference is significant at this n: val hard 12/18 against 17/18 gives
  Fisher p = 0.09, and `asks` 6/12 against 10/12 gives p = 0.19. The direction is the same on
  every measure.
- **Rescored in round 3c.** The verifier now counts `uv run graph-agents-cli eval run` (`3036936`).
  The approved text's val is 14/18 (must-proceed 9 of 9), against 17/18 p = 0.34; see
  [`review-r3c-workflow.md`](review-r3c-workflow.md). The table above is as first recorded.
- **Only R1 and R2 are backed by a failure the optimizer saw.**
  - R3 and R4 came from success trajectories, and no val task isolates their effect.
  - R4's accept (0.924 → 0.995) is within noise.
    - Step 3 no longer showed two step-1 val failures: a denied compound `eval run`, and a
      `covers-provider` miss that R4 does not touch.
    - With R4 in the body, every rollout of the tasks that must proceed still ran compound
      commands.
    - Their permission denials per val phase went from 8 to 3 to 5, which is noise.

## Diff: the optimizer's best body against the approved body

```diff
--- approved/workflow
+++ best/workflow
@@ -107,11 +107,14 @@
 `create`, `scaffold enhance`, or any agent code. Do these instead:
 
 1. Write or update a draft `.graph-agents-cli-spec.md` and leave it marked unapproved.
-2. End your answer with each open decision as an explicit question. Give the options and your
+2. End your answer with each open decision written as a direct question sentence that ends in `?`
+   (for example "Should the agent only read, or also write?"). A heading called "Open questions",
+   a recommended default, or a "please confirm X" list is not a question. Give the options and your
    recommendation. Typical open decisions: which API operations and what access, which
    credential, who calls the agent and how they authenticate, the model provider (data egress),
    and the runtime, CD mode and registry.
-3. Ask the user to approve the spec.
+3. Ask the user to approve the spec as a question ("Do you approve this spec, or what should change?"),
+   not as an instruction such as "say approved" or "once approved I will...".
 
 **Scale the ceremony to complexity:** a trivial agent (single tool, fixed persona) needs a couple
 of questions, a 2-3 sentence spec, and one approval; a complex agent (multi-step graph, external API
@@ -253,6 +256,11 @@
    - `contains` fails while the tool is called: look at the tool's return text or the prompt.
 5. Repeat until `eval run` exits 0. The exit code is the gate; a passing run has no `failed`,
    `error`, or `missing` case and every quality metric meets its `min_pass_rate`.
+6. To prove a fix or change broke nothing, report all three results: `graph-agents-cli lint`
+   (exit code), `eval run` (per-case status and exit code), and a `graph-agents-cli run` smoke test
+   whose output shows the expected tool call.
+7. Under `MODEL_PROVIDER=fake` (or a `fake` judge) the CLI warns that exit 0 is a plumbing check
+   only. Say so in your report; never present it as evidence of agent quality.
 
 Expect several iterations here.
 
@@ -356,7 +364,7 @@
 
 ### Systematic debugging
 
-1. **Reproduce:** run the exact command that failed; save the full output.
+1. **Reproduce:** before reading or editing code, run the exact command that failed as one plain command from the project root (for example `graph-agents-cli eval run`). Leave out any `cd ... &&` chain, env-var prefix (`MODEL_PROVIDER` and friends already come from `.env`), pipe, or shell loop. Sessions without an approval surface deny compound commands; if a call is denied, rerun it bare rather than skipping the reproduction. Save the full output and note which cases or checks fail before the fix.
 2. **Localize:** agent code, a tool, the policy, configuration, or the environment? Use
    `graph-agents-cli run "prompt" -v` to see every SSE event; use `playground --graph` for graph
    state; use `deploy --dry-run` for rendered manifests.
@@ -420,3 +428,6 @@
 If skills seem outdated or incomplete, reinstall with `graph-agents-cli setup` (or
 `graph-agents-cli update`). Set `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` on disconnected installs to
 silence the update and skills-version checks.
+
+<!-- SLOW_UPDATE_START -->
+<!-- SLOW_UPDATE_END -->
```

## Hunks

**R1, Phase 0 step 2: targeted fix.** This is the edit the round-2 review left to training: the
owner kept `asks` strict, so the final answer must contain the open decisions as questions.

- **How it was found.** Epoch 1 found it from three train failures. In each, the agent stopped
  before `create` and wrote a draft spec, then listed the open decisions as statements,
  recommendations or "please confirm" items. The analyst's summary: "never as direct questions, so
  the final answer had no question mark".
- **What is risky in it.**
  1. **The check's own criterion.** "a direct question sentence that ends in `?`" states what the
     verifier tests (`final_regex: \?`, `tasks/workflow/wf-spec-gate-*/task.json`). A question
     ends in a question mark, so the rule is also plain good form. It quotes no task prompt, and
     its example ("Should the agent only read, or also write?") is generic. Kept.
  2. **The residual lapse.** The rule says "End your answer with ..." but not "in the answer
     itself, not only in the spec file".
     - Bodies with R1 lapsed in 3 of 55 spec-gate rollouts this round: none of 34 in training, 2
       of 12 in the best body's head-to-head and 1 of 9 in the proposed text's confirmation.
     - All three lapses wrote the draft spec and then pointed to it: "review
       `.graph-agents-cli-spec.md`, answer or adjust the open questions, and approve it". That is
       the round-2 failure mode.
     - See *Not changed, for the owner to decide*, item 1.

**R2, Phase 0 step 3: targeted fix.** It comes from the same failure patch. "say approved" and
"once approved I will..." quote the agents' own phrasing in the failing rollouts, not the
benchmark. It makes no CLI claim. Kept.

**R3, Phase 3 steps 6-7: success-derived.** It came from step 1's success patch on the two passing
train rollouts, and condenses how they reported. What needs changing:

1. **Imprecise:** "`eval run` (per-case status and exit code)". The console shows the per-status
   counts ("Evaluation gate" table), a "Cases needing attention" table for the cases that did not
   pass, and the exit code. Every case's status is only in the results file (claims 3-4). The
   proposed text says "the per-status counts and the exit code", which matches Phase 3 step 3's
   existing "paste the per-status counts and the exit code".
2. **True, with a precision the text does not need.** "Under `MODEL_PROVIDER=fake` (or a `fake`
   judge) the CLI warns that exit 0 is a plumbing check only". The `Result:` line carries "(fake
   model: plumbing check only, not a quality signal)" whenever the agent (local server only) or a
   scoring judge ran on the fake model (claims 6-7). Kept.
3. **Prescriptive.** Every "fix or change" now closes with `lint`, `eval run` and a `run` smoke
   test. Phase 2 and Phase 3 already ask for these separately, so this adds no new obligation.
   Kept.

**R4, Systematic debugging step 1: success-derived, risky (harness).**

- **Where it came from.** It came from step 3's success-only reflection. In both debugging train
  rollouts, Claude Code's permission gate auto-denied compound commands: `cd weather-bot &&
  MODEL_PROVIDER=fake uv run graph-agents-cli eval run 2>&1 | tail -80`. The rollouts run with
  `--permission-prompts none`, so anything that would prompt is denied (known issue B4). The
  agents succeeded by rerunning the command bare.
- **What is risky in it:**
  1. **Harness wording.** "Sessions without an approval surface deny compound commands"
     paraphrases Claude Code's denial message ("this session has no approval surface"). It
     describes the benchmark's permission setup, not graph-agents-cli. The skill ships to every
     coding agent, and in an interactive session a compound command prompts rather than failing.
  2. **One claim needs checking.** "`MODEL_PROVIDER` and friends already come from `.env`" is
     true: the local server started by `run` and `eval run` gets `.env` with the environment
     winning (claim 9). So a `MODEL_PROVIDER=` prefix does override the project's setting. The
     fixed prompt prefix of the benchmark ("Generated projects here run on the deterministic fake
     model provider (MODEL_PROVIDER=fake)", `gac_skillopt/tasks.py:44-49`) is what makes agents
     add it.
  3. **No measured benefit.** Every rollout of the three tasks that must proceed ran compound
     commands, under every body. Their permission denials were 8 in the step-1 val phase, 3 at
     step 3 and 5 at step 4. In the head-to-head they were 6 for the best body against 4 for the
     approved text.
- **What stays useful.** Reproduce before reading code; run the exact failing command; keep its
  exit code visible; do not skip the reproduction when a command is refused; note which cases and
  checks fail. The proposed text keeps these, gives a product reason for the plain command (`.env`
  holds the settings, and a pipe hides the exit code), drops the harness sentence and rewraps the
  one 503-character line to the file's 100-column style.

**R5, the slow-update placeholder: artefact.**

- **What it is.** SkillOpt's first-epoch slow update "injected empty placeholder": two HTML
  comments, 53 characters, at the end of the body.
- **Why remove it.** Agents never see HTML comments rendered, but the markers are optimizer
  bookkeeping. The epoch-2 slow update would have filled them with a 3,693-character "Strategic
  guidance (protected)" section, which the fact-check rejected on size.

### Claims checked against the code

| # | Claim (hunk) | Verdict | Evidence |
|---|---|---|---|
| 1 | R1 and R2 make no CLI claim; the draft spec stays "marked unapproved" (the approved text's own step 1) | no claim | - |
| 2 | `graph-agents-cli lint` reports an exit code (R3) | true: 0 clean; 1 for a refused call, an unreadable `API_CALLS` or ruff; 3 for a configuration error | `src/graph_agents_cli/dev/cmd_lint.py:51-55` (documented codes), `:70` (`ClickException`, exit 1); registered at `src/graph_agents_cli/main.py:518` |
| 3 | `eval run` prints the per-status counts and the exit code (R3) | true | `src/graph_agents_cli/eval/cmd_run.py:356` (`grade_traces`) → `src/graph_agents_cli/eval/cmd_grade.py:380` → `src/graph_agents_cli/eval/gate.py:348-361` (the "Evaluation gate" status table), `:420` (`Result: ... (exit code N)`) |
| 4 | ... "per-case status" (R3, best body) | **imprecise** | the console lists only the cases that did not pass (`eval/gate.py:392-405`, "Cases needing attention"); every case's status is in the results file (`eval/cmd_grade.py:369`). Reworded in the proposed text |
| 5 | a `graph-agents-cli run` smoke test's output shows the tool call (R3) | true | `src/graph_agents_cli/run/cmd_run.py:314-320` prints `[tool_call: <name>(<args>)]` for every tool-call event, without `-v`. Under the fake provider the model calls a bound tool only when the prompt names it or a distinctive word of its name (`src/graph_agents_cli/scaffold/agents/langgraph/app/app_utils/model.py:228-229`, `:284-296`) |
| 6 | under `MODEL_PROVIDER=fake` the CLI says exit 0 is a plumbing check only (R3) | true | `src/graph_agents_cli/eval/gate.py:415-420`: "(fake model: plumbing check only, not a quality signal)" on exit 0 when `fake_model` is set; the warning text is `eval/cmd_grade.py:179-183` ("this run proves the eval plumbing only") |
| 7 | ... "(or a `fake` judge)" (R3) | true | `eval/cmd_grade.py:197-211`: `fake_model` includes `judge` when a judge metric scored something with provider `fake`, and `agent` only for traces from the project's own local server; `FAKE_JUDGE_WARNING` at `:173-177` |
| 8 | `eval run` can be run from the project root (R4) | true, and not required | the CLI finds the project by walking up from the current directory (`src/graph_agents_cli/_project.py:372-379`); `lint` changes to the project root itself (`dev/cmd_lint.py:57`) |
| 9 | `MODEL_PROVIDER` and the other settings come from `.env` (R4) | true | `src/graph_agents_cli/run/_local_server.py:642-647`: the local server's environment is `{**dotenv_settings(project_root / ".env"), **os.environ}`, so the environment wins and a prefix overrides `.env` |
| 10 | "Sessions without an approval surface deny compound commands" (R4, best body) | **not a product fact** | Claude Code's behaviour under `--permission-prompts none`, as the benchmark runs it (`tools/skillopt/gac_skillopt/isolation.py:410-420`, `:472`); the denial text in the traces reads "this session has no approval surface". Removed in the proposed text |
| 11 | a pipe hides the command's exit code (R4, proposed text) | true (shell) | a pipeline's status is its last command's unless `pipefail` is set; the benchmark's own traces show `... eval run 2>&1 \| tail -80` |

### Changes made in the proposed final text

Each change is labelled. The optimizer's other text is kept word for word.

| Change | Why | Kind |
|---|---|---|
| R3: "`eval run` (per-case status and exit code)" → "`eval run` (the per-status counts and the exit code)"; the lines rewrapped | claim 4 | factual precision |
| R4: "Leave out any `cd ... &&` chain, env-var prefix (`MODEL_PROVIDER` and friends already come from `.env`), pipe, or shell loop. Sessions without an approval surface deny compound commands; if a call is denied, rerun it bare rather than skipping the reproduction." → "..., with no `cd ... &&` chain, env-var prefix, pipe or loop: the project's settings, `MODEL_PROVIDER` included, come from `.env`, and a pipe hides the command's exit code. If a command is refused, rerun it bare rather than skipping the reproduction." | claim 10: the harness sentence goes, and the plain-command rule gets product reasons (claims 9, 11) | generalisation |
| R4: the 503-character line rewrapped to the file's 100-column style | style | layout |
| R5: the two `SLOW_UPDATE` comments removed | optimizer bookkeeping, not guidance | artefact removal |

`diff` of the proposed text against the approved text (what a write-back would change):

```diff
--- approved/workflow
+++ final/workflow
@@ -107,11 +107,14 @@
 `create`, `scaffold enhance`, or any agent code. Do these instead:
 
 1. Write or update a draft `.graph-agents-cli-spec.md` and leave it marked unapproved.
-2. End your answer with each open decision as an explicit question. Give the options and your
+2. End your answer with each open decision written as a direct question sentence that ends in `?`
+   (for example "Should the agent only read, or also write?"). A heading called "Open questions",
+   a recommended default, or a "please confirm X" list is not a question. Give the options and your
    recommendation. Typical open decisions: which API operations and what access, which
    credential, who calls the agent and how they authenticate, the model provider (data egress),
    and the runtime, CD mode and registry.
-3. Ask the user to approve the spec.
+3. Ask the user to approve the spec as a question ("Do you approve this spec, or what should change?"),
+   not as an instruction such as "say approved" or "once approved I will...".
 
 **Scale the ceremony to complexity:** a trivial agent (single tool, fixed persona) needs a couple
 of questions, a 2-3 sentence spec, and one approval; a complex agent (multi-step graph, external API
@@ -253,6 +256,11 @@
    - `contains` fails while the tool is called: look at the tool's return text or the prompt.
 5. Repeat until `eval run` exits 0. The exit code is the gate; a passing run has no `failed`,
    `error`, or `missing` case and every quality metric meets its `min_pass_rate`.
+6. To prove a fix or change broke nothing, report all three results: `graph-agents-cli lint`
+   (exit code), `eval run` (the per-status counts and the exit code), and a `graph-agents-cli run`
+   smoke test whose output shows the expected tool call.
+7. Under `MODEL_PROVIDER=fake` (or a `fake` judge) the CLI warns that exit 0 is a plumbing check
+   only. Say so in your report; never present it as evidence of agent quality.
 
 Expect several iterations here.
 
@@ -356,7 +364,12 @@
 
 ### Systematic debugging
 
-1. **Reproduce:** run the exact command that failed; save the full output.
+1. **Reproduce:** before reading or editing code, run the exact command that failed as one plain
+   command from the project root (for example `graph-agents-cli eval run`), with no `cd ... &&`
+   chain, env-var prefix, pipe or loop: the project's settings, `MODEL_PROVIDER` included, come
+   from `.env`, and a pipe hides the command's exit code. If a command is refused, rerun it bare
+   rather than skipping the reproduction. Save the full output and note which cases or checks
+   fail before the fix.
 2. **Localize:** agent code, a tool, the policy, configuration, or the environment? Use
    `graph-agents-cli run "prompt" -v` to see every SSE event; use `playground --graph` for graph
    state; use `deploy --dry-run` for rendered manifests.
```

## Not changed, for the owner to decide

1. **The residual `asks` lapse (reviewer-proposed; not measured and not in the proposed text).**
   R1 could end "..., in the answer itself, not only in the spec file". All three lapses this
   round pointed to the spec file for the questions. Round 2 held this wording back so that
   training could find it, and a full run did not. Measuring it costs about 18 sessions (the three
   `asks`-prone val tasks × 3 repetitions, plus the tasks that must proceed); the test split is
   spent for this round.
2. **Whether to keep R4 at all.** The proposed text keeps a product-worded R4 because its advice is
   sound, but nothing measured shows that it helps. Dropping it saves 429 characters and loses
   nothing measurable.
3. **Two things in the benchmark flatter the best body; neither is in the skill.**
   - The `proved` check rejects `uv run graph-agents-cli eval run` (2 of the approved text's 6
     head-to-head val failures). Fixing the check is a small change to `verify.py`
     `command_segments`, followed by `baseline rescore`.
   - The permission-gate noise (B4) stays open.
4. **Rejected candidates worth a human look (not proposed).**
   - Step 2 was lost to a whitespace mismatch. It would have made the `contains` bullet say:
     compare the expected string with the tool's literal return text.
   - Step 4 was rejected (0.957). It says scaling the ceremony down "drops questions about style,
     not questions about risk", and that a `fake` provider in `.env` does not settle the provider
     question.
   - The epoch-2 slow update's rename checklist was rejected by the fact-check on size.

   Each is plausible guidance, and none was measured to help.
5. **Where it lands.** The approved round-3a text is being written back on `v0.3` (round-3b
   decision 2). A write-back of this text would replace that body in both copies on `v0.3`, with
   its own CHANGELOG line.

## Confirmation of the final text

The proposed text was run on val in `acceptEdits`, 3 repetitions in parallel (18 rollouts, 8.8
min):

- **Val:** hard 17/18 (0.944 ± 0.096), soft 0.970 ± 0.003. The optimizer's best body scored the
  same 17/18 in its fresh head-to-head.
- **Spec-gate tasks:** all 9 stopped before `create`, and 8 of 9 passed `asks`. The one lapse
  (`wf-spec-gate-slack-digest` rep 2) wrote the spec and ended "review `.graph-agents-cli-spec.md`,
  answer or adjust the open questions, and approve it".
- **Tasks that must proceed:** 9 of 9. The reworded R4 does not over-stop, and no `proved` false
  negative occurred.
- **Denials:** the tasks that must proceed had 2 permission denials, and 3 rollouts had one in
  all. That is the fewest of any val run this round, but at this n it is not evidence for the
  rewording.
- **Test was not rerun.** The frozen test task was used once, for the approved and the best body.

**Sessions:** 18 Claude Code sessions, about $3.86 API-equivalent. The training, the head-to-head
and this confirmation used 174 sessions together ([`train-r3-workflow.md`](train-r3-workflow.md)).

## Write-back, once approved (not done)

1. Replace the body of `skills/graph-agents-cli-workflow/SKILL.md` with the approved text, on
   `v0.3` after the round-3a write-back, keeping the frontmatter. Copy the file to
   `src/graph_agents_cli/skills/data/graph-agents-cli-workflow/SKILL.md`; the two copies must
   stay byte-identical.
2. Add a CHANGELOG entry under Unreleased that credits the SkillOpt experiment.
3. Run the fast suite, `ruff`, and `mkdocs build --strict` if the site quotes the skill.

## Proposed `SKILL.md` (complete file)

The file below round-trips exactly (sha256 `12d24bf9d7ac7eda…`): its frontmatter is the approved
file's, and its body is the proposed final body.

````markdown
---
name: graph-agents-cli-workflow
description: >
  This skill should be used when the user wants to "develop an agent",
  "build an agent with LangGraph", "build a LangGraph agent", "run the agent
  locally", "debug agent code", "test an agent", "evaluate an agent",
  "deploy an agent to Kubernetes", "monitor an agent", or needs the
  graph-agents-cli development lifecycle and coding guidelines.
  Entrypoint for building LangGraph agents with graph-agents-cli.
  Always active: provides the full workflow (understand, scaffold, build,
  evaluate, deploy, observe), process deference to a project's declared
  process, the spec-before-code gate, code preservation rules, the
  never-change-the-model rule, human approval before deploy, and the
  3-strikes loop breaker.
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
---

# Agent Development Workflow and Guidelines

**graph-agents-cli** is a CLI and skills toolkit for building, evaluating, and deploying
[LangGraph](https://langchain-ai.github.io/langgraph/) agents on self-hosted Kubernetes. It works
with any coding agent (Claude Code, Codex, Gemini CLI, Cursor, Antigravity, others). The agent's
model is a scaffold-time and runtime choice among OpenAI, Anthropic, Gemini (AI Studio API key),
and any OpenAI-compatible endpoint (Ollama, vLLM, TGI, OpenRouter). It is generic: projects pick an
auth policy and declare their outbound APIs, nothing is tied to one consumer. Install with
`uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0` and `graph-agents-cli setup`.

> **Before writing agent code, make sure a scaffolded project exists (see Phase 1).** Skipping the
> scaffold loses the chat API, the auth policy adapter, the eval gate, the Helm chart, and the
> CI workflows the template wires up.

> Requires: graph-agents-cli ~= 0.2.0. Check with `graph-agents-cli --version` or
> `graph-agents-cli info`. [Install uv](https://docs.astral.sh/uv/getting-started/installation/index.md)
> first if needed.

## Session continuity and skill cross-references

Re-read the relevant skill **before** each phase, not after you have started and hit a problem.
Context compaction may have dropped earlier skill content. If skills are missing, run
`graph-agents-cli setup` to install them.

| Phase | Skill | When to load |
|-------|-------|--------------|
| 0 - Understand | this skill, `references/brainstorming.md` | Read the project's process document if one is declared (see *Process deference*), else `.graph-agents-cli-spec.md` if present, else clarify goals with the user |
| 1 - Scaffold | `/graph-agents-cli-scaffold` | Before creating, enhancing, or upgrading a project |
| 2 - Build | `/graph-agents-cli-langgraph-code` | Before writing agent code: graph, tools, checkpointer, streaming, interrupts, auth policy, API client |
| 3 - Evaluate | `/graph-agents-cli-eval` | Before running any eval: dataset schema, expect checks, judge metrics, the gate rule and exit codes |
| 4 - Deploy | `/graph-agents-cli-deploy` | Before deploying: modes, environments, secrets, GitOps PR flow, GitHub settings, `infra check` |
| 5 - Observe | `/graph-agents-cli-observability` | After deploying: tracing opt-in, capture policy, LangSmith or OTLP, run records |

---

## Setup

If `graph-agents-cli` is not installed:

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0   # a release tag; not on PyPI yet
graph-agents-cli setup          # installs the six skills into detected coding agents
```

`uv` missing: follow the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/index.md).

Users name things inconsistently ("Agent Server", "GitOps", "air-gapped", "Studio"). Map user terms
to CLI values with `references/terminology.md`.

---

## Process deference (read this before Phase 0)

A consuming project may govern agent work through its **own process** (for example a document
chain such as BRD -> PRD -> TRD/ADRs -> epics/stories -> acceptance cases). graph-agents-cli
records that in two places:

- the project manifest `graph-agents-cli-manifest.yaml`, key `process:` (a path to the governing
  process document, or `null`); this is what `info` and `scaffold upgrade` read;
- the project guidance file (`AGENTS.md` by default, or `CLAUDE.md` / `GEMINI.md`), which renders the same
  value; this is what you, the coding agent, read.

**Rule.** If the guidance file or the manifest declares `process:`:

1. Read the named process document first and follow **its** gates, roles, and approval sequence
   for everything in this skill (design, scaffolding, coding, evaluation, deployment).
2. Treat this skill's generic spec-before-code gate (`.graph-agents-cli-spec.md`) as **satisfied
   only by that process's own approvals**. Do not write a `.graph-agents-cli-spec.md` as a
   substitute for the process's documents, and never treat an approved spec as permission the
   process has not given.
3. Where the process is silent, the rules below still apply (code preservation, never change the
   model, human approval before deploy, the eval gate, the 3-strikes breaker).
4. Choices that the process owns stay with the process: the outbound API policy
   (`api-policy.yaml`), which credentials and roles the auth policy validates, data-egress and
   trace-capture decisions, and whether the project may be deployed at all.

If no process is declared, the generic gate in Phase 0 applies.

---

## Phase 0: Understand

Before scaffolding or writing anything, understand what you are building through a **design
dialogue**, not a checklist. Load `references/brainstorming.md` and follow it: ask **one question
at a time**, propose 2-3 architecture approaches for non-trivial agents, and validate the design
before any scaffolding.

If `.graph-agents-cli-spec.md` exists in the project directory (and no process is declared), read
it; it is your primary source of truth. Otherwise:

**Do NOT proceed to scaffolding or coding until the user approves the spec** (or, under a declared
process, until that process's approvals exist). Do not assume, research, or fill in the blanks on
your own; the user's intent drives everything.

**What counts as approval.** Only the user explicitly approving the spec, in the conversation.
None of these is approval:

- a request to build the agent, however direct;
- a spec file whose status says draft, or that still lists open questions;
- defaults you chose yourself, even safe ones, even if you recorded them in the spec as
  assumptions;
- an instruction to proceed on your own, to decide for yourself or to do what is safe while
  nobody can answer your questions: in that session the safe choice is to stop at the spec.

When the spec is not approved and nobody can approve it, the safe action is to stop before
`create`, `scaffold enhance`, or any agent code. Do these instead:

1. Write or update a draft `.graph-agents-cli-spec.md` and leave it marked unapproved.
2. End your answer with each open decision written as a direct question sentence that ends in `?`
   (for example "Should the agent only read, or also write?"). A heading called "Open questions",
   a recommended default, or a "please confirm X" list is not a question. Give the options and your
   recommendation. Typical open decisions: which API operations and what access, which
   credential, who calls the agent and how they authenticate, the model provider (data egress),
   and the runtime, CD mode and registry.
3. Ask the user to approve the spec as a question ("Do you approve this spec, or what should change?"),
   not as an instruction such as "say approved" or "once approved I will...".

**Scale the ceremony to complexity:** a trivial agent (single tool, fixed persona) needs a couple
of questions, a 2-3 sentence spec, and one approval; a complex agent (multi-step graph, external API
access, per-user identity and roles, safety-critical) gets the full treatment in `references/brainstorming.md`.

**Topics to cover** (one question at a time):

1. **What problem will the agent solve?** Core purpose, capabilities, who calls it.
2. **External APIs or data sources?** Which API operations the agent may call, with which
   methods, and with what credential (none, a service token, or the caller's own). Access is the
   user's explicit choice per API (read-only, read-write, or a custom list of methods, then the
   allowed and denied operations): never assume one. Every outbound API is declared in
   `api-policy.yaml` with `graph-agents-cli api add` (see `/graph-agents-cli-langgraph-code`);
   the agent never gets a generic "call any endpoint" tool.
3. **Safety constraints?** What the agent must NOT do; which API calls need a human's approval
   before they are sent, and whose (the user confirming their own call, or a second person
   holding a role: an `approval` block, `graph-agents-cli api approval`); what may leave the
   network (model egress, traces).
4. **Model provider?** `openai`, `anthropic`, `gemini`, or `openai-compatible` (on-network servers
   such as vLLM, Ollama, TGI). Selecting a hosted provider sends prompts, tool results, and assembled
   context to that provider; the user must decide that explicitly.
5. **Deployment preference?** Prototype first (recommended, `--prototype`, no deployment files) or
   Kubernetes from the start. If Kubernetes: runtime `fastapi` (default) or `langgraph-server`;
   CD mode `skip`, `helm-push`, or `argocd`; registry; auth policy `shared-bearer`, `jwt` or
   `custom`.

**Ask based on context:**

- Persistent conversations across restarts or replicas: `--checkpointer postgres` (the default
  for Kubernetes); local development uses `CHECKPOINTER=memory` from `.env` and needs no database.
- Callers are individual users with an OIDC identity provider: `--auth-policy jwt` (per-user
  principals from verified tokens). Callers already carry another credential (for example an
  existing application's session cookie): `--auth-policy custom`; the template ships the
  interface and a stub that fails closed until the project implements it.
- Disconnected or air-gapped cluster: the **disconnected profile** (`openai-compatible` model and
  judge on-network, runtime `fastapi`, tracing off or OTLP in-cluster, `cd: skip` unless an
  on-network GitHub Enterprise Server exists). See `/graph-agents-cli-deploy`.
- Other agents must call this one: A2A is built into every scaffolded app; scaffold normally.
- CI/CD wanted: does a GitHub repository exist? Creating one (public or private) needs the user's
  say-so.

Once the design is agreed, write `.graph-agents-cli-spec.md` from `references/spec-template.md`,
self-review it, then get the user's approval. `/graph-agents-cli-scaffold` maps the choices to flags.

## Phase 1: Scaffold

Check whether a project already exists: run `graph-agents-cli info` from the project root. If it
was created or enhanced by graph-agents-cli, skip this phase.

Otherwise scaffold **before writing any code**:

- **No project yet:** `graph-agents-cli create <name> ...` (alias of `scaffold create`)
- **Existing code to import:** `graph-agents-cli scaffold enhance .`
- **Older scaffold:** `graph-agents-cli scaffold upgrade`

Use `/graph-agents-cli-scaffold` for every flag, the valid runtime x checkpointer x target
combinations, prototype semantics, and what `upgrade` never touches.

## Phase 2: Build and implement

1. Read the project's guidance file for the agent directory (default `app/`).
2. Edit only agent code: `app/agent.py` (exports `graph`, an unbound compiled `StateGraph`),
   `app/tools/**`, `app/policies/**`, and the reserved `app/prompts/**` and `app/graph/**`
   directories you may create. `upgrade` never modifies these.
3. **Smoke test:** `graph-agents-cli run "your prompt"` starts the local server for the project's
   runtime, sends one chat message over the same `/chat` SSE API your client application will call, and prints
   the reply. Use `--start-server` when iterating on several prompts, and `--thread-id` to continue
   a thread (the footer prints the thread id and the resume command, also after an error).
   `-v` adds one line per SSE event (tool calls, results, usage). Under the `jwt` policy the
   server needs a token: `graph-agents-cli auth dev-token --sub <user> [--roles r1,r2]` writes a
   dev key to `.env` (`APP_ENV=dev` only) and prints a token; put it in
   `GRAPH_AGENTS_CLI_API_KEY` (`export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token
   --sub alice)"`), which `run` and `eval` send as the bearer. Never pass a token with `--header`:
   argv is visible to other users and lands in shell history.
4. Interactive testing: `graph-agents-cli playground` (the selected application with reload and
   the `/playground` dev page). `playground --graph` opens LangGraph Studio through `langgraph dev`;
   it bypasses the auth policy and the chat API, so use it for graph debugging only.
5. `graph-agents-cli lint` runs ruff and the API-policy check: every `*.py` under `app/tools/`
   (subpackages included) declares one literal `API_CALLS` (and `TOOLS`), which the CLI reads
   statically with `ast` and checks against `api-policy.yaml` (and the API's OpenAPI spec when it
   names one). A refused call comes with the `graph-agents-cli api` command that would allow it:
   propose that change to the user (it widens access, so it needs their approval and a reviewed
   pull request); never run it on your own.
6. **Adding functionality to a working agent** follows the same loop: agree the new operations
   and their access with the user, change the policy with `graph-agents-cli api` (`allow`,
   `access`; `--dry-run` first, show the diff), write the tool with its `API_CALLS`, `api check`
   (or `lint`), decide with the user whether the new writes wait for a human (`api approval`;
   eval cases then say how each gate is decided), add eval cases and run `eval run`, then a pull
   request (CODEOWNERS approves `api-policy.yaml`), build and deploy dev, staging, prod. The policy is baked into the image,
   so what passed staging is what reaches production. On an API without `allowed_operations`
   (every operation within its methods), `allow` the operations the agent already calls
   before the new one: the first `allow` creates the list and refuses every call not on it,
   and `access` alone would open a new method to every operation of the API.

Load `/graph-agents-cli-langgraph-code` for `create_agent` versus explicit `StateGraph`, tools and
their `API_CALLS` declaration, checkpointers and `thread_id`, streaming events, interrupts
(a LangGraph pattern; resume over `/chat` is not implemented in this milestone), subgraphs,
`init_chat_model` provider switching, the deterministic `fake` provider for tests, the auth policy
adapter, the API client, and telemetry.

> **Smoke-test only here; do not write behavioural unit tests.** Model output is
> non-deterministic; behavioural checks belong in eval (Phase 3), not in `pytest`. Unit tests may
> cover tools, the API client, and policy code with the `fake` provider.

## Phase 3: Evaluate

**This is the most important phase.** Evaluation validates agent behaviour end to end, and the
gate is enforceable: `eval run` exits non-zero when the gate is not met, and `pr_checks` treats
that as a failed check.

**MANDATORY:** load `/graph-agents-cli-eval` before running evaluation. It has the dataset schema,
the expect checks, the judge metrics, the gate rule, and the exit codes.

**Unit tests versus `graph-agents-cli eval`:**

- **Unit tests** (`uv run pytest`) test code correctness: imports, tool functions, policy
  enforcement, the API client, with the `fake` model provider. They never test whether the
  agent behaves well.
- **`graph-agents-cli eval run`** tests agent behaviour: response content, tool trajectories,
  latency, tokens, and subjective quality through a model judge.
- **`graph-agents-cli run "prompt"`** is a one-off smoke test during development.

**NEVER write unit tests that assert on model response content.** Put those checks in an eval case
(`expect.contains`, `expect.tool_calls`, a judge metric) instead.

1. Start small: 1-2 cases in `tests/eval/datasets/`. Under `jwt`, export the dev token first
   (Phase 2, step 3): `eval run` sends `GRAPH_AGENTS_CLI_API_KEY` like `run` does.
2. `graph-agents-cli eval run` (chains `generate` and `grade`). For debugging use `eval generate`
   then `eval grade` on the traces file.
3. Discuss results with the user; paste the per-status counts and the exit code.
4. Fix issues; iterate on the core cases first, then add edge cases. When an eval that passed
   before breaks, fix the agent, not the eval: do not edit `tests/eval/` (datasets,
   expectations, `min_pass_rate` thresholds) to reach exit 0, unless the user asked for the
   change the eval checks (for example, a renamed tool or argument). Map each failed check to
   code:
   - `tool_calls` with no actual calls: the tool is not registered. `app/tools/__init__.py`
     collects the `TOOLS` list of every module under `app/tools/`; a module without `TOOLS` (or
     with it renamed) contributes no tools, and nothing warns about it.
   - `contains` fails while the tool is called: look at the tool's return text or the prompt.
5. Repeat until `eval run` exits 0. The exit code is the gate; a passing run has no `failed`,
   `error`, or `missing` case and every quality metric meets its `min_pass_rate`.
6. To prove a fix or change broke nothing, report all three results: `graph-agents-cli lint`
   (exit code), `eval run` (the per-status counts and the exit code), and a `graph-agents-cli run`
   smoke test whose output shows the expected tool call.
7. Under `MODEL_PROVIDER=fake` (or a `fake` judge) the CLI warns that exit 0 is a plumbing check
   only. Say so in your report; never present it as evidence of agent quality.

Expect several iterations here.

## Phase 4: Deploy

Once the user agrees the eval gate is met:

1. `graph-agents-cli info` shows the deployment target, runtime, CD mode, registry, and auth policy.
2. Prototype (`deployment_target: none`)? Add deployment first:
   `graph-agents-cli scaffold enhance . --deployment-target kubernetes [--cd ...]`.
3. `graph-agents-cli infra check --env <env>` reports the cluster and repository prerequisites for
   the project's mode (read-only, never creates anything).
4. Secrets: `graph-agents-cli secrets apply --env <env>` reads `.env.<env>` (only `dev` falls
   back to `.env`); in `helm-push` and `argocd` modes the named owner provisions them from a
   workstation, never CI. `secrets status --env <env>` exits 0 when every required key is there.
5. `graph-agents-cli deploy --env <env>`; what that does depends on the CD mode (direct helm,
   helm from a CI runner, or a pull request that Argo CD reconciles). Outside `dev` the kube
   context must be recorded in the manifest or passed with `--context`; never pass `--yes` to
   accept the current context without showing it to the user. `--dry-run` prints every command
   and the rendered manifests without running them.

**IMPORTANT: never deploy without explicit human approval.** In `argocd` mode a production change
is a PR that a code owner merges; the merge is the single gate and you never merge it yourself.
`/graph-agents-cli-deploy` has the mode table, environments, rotation, and the required GitHub
settings.

## Phase 5: Observe

Tracing is **off** unless `TRACING_ENABLED=true`; `TRACE_CAPTURE` defaults to `metadata` (no
prompt or tool text). See `/graph-agents-cli-observability` for LangSmith versus OTLP, the capture
policy, hashed principal ids, and run records.

---

# Operational guidelines for coding agents

## Common shortcuts to resist

| Shortcut | Why it fails |
|----------|-------------|
| "The request is clear enough, no need to clarify" | You are guessing at requirements. Phase 0 (or the project's process) exists to confirm intent before scaffolding. |
| "The project has a process document, but a quick spec is faster" | The process owns the gates. A generic spec cannot stand in for the approvals it requires. |
| "It answered correctly in `run`, so eval is unnecessary" | One prompt is not a test suite. The eval gate catches regressions, tool trajectory errors, and edge cases. |
| "I'll switch to a newer/better model" | The provider and model were chosen deliberately and written to `.env` and the manifest. Changing them without being asked violates code preservation and is an egress decision the user owns. |
| "I'll add a generic HTTP tool so the agent can call whatever it needs" | `app_utils.api_client` is the only path to external APIs and it enforces `api-policy.yaml`. A generic tool bypasses the policy the team reviewed. |
| "The tool needs POST, I'll widen the API's access" | Widening access is the user's decision and a reviewed change. Propose the `graph-agents-cli api` command `lint` prints; run it only when asked. |
| "The approval prompt is in the way, I'll approve it / remove the gate" | Deciding a gated call is the approver's act, and loosening a gate is a reviewed change like widening access. Show the user the call and the `approvals` commands; never decide for them. |
| "I'll `helm upgrade` / `kubectl apply` directly, it's quicker" | In `argocd` mode the cluster follows `main`; direct changes are drift that self-heal reverts, and they skip the production gate. |
| "I can skip the scaffold and set up manually" | Manual setup misses the chat API, auth adapter, eval gate, chart, and workflows. Use `create` even for experiments (`--prototype`). |

## Principle 1: code preservation and isolation

Change only the lines the user's request targets; preserve everything else (code, configuration
values such as `MODEL_PROVIDER`, `MODEL_NAME`, `CHECKPOINTER`, comments, formatting).

**Before finalizing any edit, verify:**

1. **Target identification:** the exact lines to change, from the user's explicit instruction only.
2. **Preservation check:** everything outside the target is identical.

Example. User: "Change the system prompt to a recipe suggester."

```python
# VIOLATION: the model was not requested to change
graph = create_agent(
    model=init_chat_model("openai:gpt-5"),  # replaced get_model() -- NOT asked
    tools=TOOLS,
    system_prompt="You are a recipe suggester.",
)

# COMPLIANT
graph = create_agent(
    model=get_model(),  # PRESERVED: reads MODEL_PROVIDER / MODEL_NAME
    tools=TOOLS,  # PRESERVED
    system_prompt="You are a recipe suggester.",  # the direct target
)
```

## Principle 2: execution best practices

- **Model selection (CRITICAL):**
  - **NEVER change the model or provider unless explicitly asked.** The model is configured by
    `MODEL_PROVIDER` and `MODEL_NAME` in `.env` and the chart, never in code.
  - New projects get the provider default that `create` records. Do not hard-code model names from
    memory; your training data is likely out of date. If the user wants a different model, change
    `MODEL_NAME` in `.env` (and the manifest through `scaffold enhance` when relevant), not `agent.py`.
- **Running Python:** always through `uv` (`uv run python ...`, `uv run pytest`). Run
  `graph-agents-cli install` (which is `uv sync`) after dependency changes.
- **3-strikes loop breaker:**
  - **Stop immediately** if you see the same error three times in a row.
  - Red flags: retrying the same `deploy`, incrementing image tags v5 -> v6 -> v7, "I'll try one
    more time" repeatedly, re-running `eval` hoping the judge scores differently.
  - When stuck: run the underlying command directly (`references/internals.md` says which:
    uvicorn, `langgraph dev`, `docker build`, `helm template`, `kubectl`, `gh`), read its output,
    and report to the user instead of retrying.
- **Troubleshooting:**
  - `/graph-agents-cli-langgraph-code` first; it covers the template contract and the patterns.
  - `graph-agents-cli <command> --help` ends with a `Source:` line pointing at the file that
    implements the command. Read it. `graph-agents-cli info` prints the CLI install path.
  - For LangGraph and LangChain API questions, fetch the upstream docs rather than guessing.

### Systematic debugging

1. **Reproduce:** before reading or editing code, run the exact command that failed as one plain
   command from the project root (for example `graph-agents-cli eval run`), with no `cd ... &&`
   chain, env-var prefix, pipe or loop: the project's settings, `MODEL_PROVIDER` included, come
   from `.env`, and a pipe hides the command's exit code. If a command is refused, rerun it bare
   rather than skipping the reproduction. Save the full output and note which cases or checks
   fail before the fix.
2. **Localize:** agent code, a tool, the policy, configuration, or the environment? Use
   `graph-agents-cli run "prompt" -v` to see every SSE event; use `playground --graph` for graph
   state; use `deploy --dry-run` for rendered manifests.
3. **Fix one thing** at a time.
4. **Verify** by re-running the reproduction.
5. **Guard** with an eval case (behaviour) or a unit test (code).

**Stop-the-line rule:** if a change breaks something that worked, fix the regression before
continuing feature work.

If a test fails in code your change did not touch, show that it is unrelated before you move on.
Grep the failing test for the identifiers you changed, then rerun that test alone. Report it as
pre-existing, with that evidence and the failure output. Do not edit unrelated code or tests to
make the failure go away.

- **Environment variables:** `.env`, `.env.<env>`, and the manifest are essential configuration;
  never remove or rewrite entries unless the user asks. Never commit `.env` files. Secrets reach
  the cluster only through `secrets apply` from the allow-listed keys in the manifest
  (`secrets.keys`), never through values files or CI.

---

## Using a temporary scaffold as reference

When you need specific files (Dockerfile, chart, workflows) without touching the current project,
create a reference project in a temporary directory with `/graph-agents-cli-scaffold` and copy what
you need.

---

## Not covered by this skill

- LangGraph and LangChain API details: `/graph-agents-cli-langgraph-code`.
- Scaffold flags and the combination table: `/graph-agents-cli-scaffold`.
- Dataset schema, judge configuration, gate exit codes: `/graph-agents-cli-eval`.
- Deployment modes, secrets, GitOps, GitHub settings, `infra check`: `/graph-agents-cli-deploy`.
- Tracing destinations and capture policy: `/graph-agents-cli-observability`.
- Any cloud-managed agent runtime, registry, or publishing catalog: graph-agents-cli has none.

## Migration note

graph-agents-cli is a fork of google-agents-cli (ADK on Google Cloud). ADK became LangGraph;
Agent Runtime, Cloud Run, and GKE became any Kubernetes cluster via Helm; Cloud Trace and BigQuery
analytics became LangSmith or OpenTelemetry behind an opt-in; the Gemini Enterprise `publish`
command was removed; gcloud authentication became provider keys plus a kubeconfig. If a user asks
for one of the old names, `references/terminology.md` maps it.

## Reference files

| File | Contents |
|------|----------|
| `references/commands.md` | Every command with its flags |
| `references/internals.md` | What each command runs under the hood (uvicorn, `langgraph dev`, docker, helm, kubectl, gh) |
| `references/terminology.md` | User terms to CLI values; migration mapping of old names |
| `references/extension.md` | Author an ad-hoc extension or adopt an existing one (override or add commands) |
| `references/spec-template.md` | `.graph-agents-cli-spec.md` template |
| `references/brainstorming.md` | Phase 0 design-dialogue playbook |

## Skills version

If skills seem outdated or incomplete, reinstall with `graph-agents-cli setup` (or
`graph-agents-cli update`). Set `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` on disconnected installs to
silence the update and skills-version checks.
````
