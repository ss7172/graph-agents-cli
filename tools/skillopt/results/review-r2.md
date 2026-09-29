# Human review of the round-2 SkillOpt candidates (workflow, scaffold)

Date: 2026-09-28. Branch `experiments/skillopt`. This page reviews the two best skill bodies of the
round-2 smoke train ([`smoke-train-r2.md`](smoke-train-r2.md)). **Nothing has been written back.**
`skills/` and `src/graph_agents_cli/skills/data/` are unchanged and still byte-identical. The
proposed final text for each skill, ready for the owner to approve, is in
[`review-r2-workflow.md`](review-r2-workflow.md) and [`review-r2-scaffold.md`](review-r2-scaffold.md).

## What was reviewed

| Skill | Shipped body | Optimizer's best body (r2 smoke train) | Proposed final body |
|---|---|---|---|
| workflow | 23,793 chars, sha256 `e0aacb800aa22ea5` | step 2, 25,561 chars (+7.4 %), `8d43923820d02f31` | 25,904 chars (+8.9 %), `efe6ca364df8d20e` |
| scaffold | 23,644 chars, `770de0aad91ebe1f` | step 3, 24,803 chars (+4.9 %), `c92011dc98051905` | 24,761 chars (+4.7 %), `2b78aeb982d77e43` |

- The "body" is `SKILL.md` without its frontmatter (`tasks.split_skill`). The frontmatter is held
  fixed and is identical in the proposed files.
- The optimizer's bodies are `r2-train/bodies/{workflow,scaffold}.md` in the round-2 scratch. The
  provenance below comes from each step's `step_record.json`, `ranked_edits.json` and
  `edit_apply_report.json`.
- Both the best and the final bodies pass the benchmark's fact-check gate
  (`factcheck.check_candidate`: sections kept, no unknown command or flag).

**How the hunks are classified:**

- **targeted fix**: a failure-derived edit that addresses one of the round-1 target failures;
- **success-derived addition**: written by success-only reflection from trajectories that already
  passed;
- **risky**: the hunk, or part of it, has a factual error, wording copied from the benchmark, a
  misplacement, or a rule that could block legitimate work.

## Summary

| Skill | Hunk | Class | Provenance | Verdict for write-back |
|---|---|---|---|---|
| workflow | W1 Phase 0, "What counts as approval" | **targeted fix**, with risky wording | step 1, failure patch, support 2 (`wf-spec-gate-draft-open-questions`, `wf-spec-gate-it-kb` built the agent) | keep; generalise two bullets and scope the stop rule |
| workflow | W2 Phase 3, eval loop and failure-to-code map | success-derived, **risky** | step 2, success-only reflection, support 3 (the two debug trajectories) | keep; drop "literal", and bound "never edit the eval" |
| workflow | W3 Systematic debugging, unrelated failing test | success-derived | step 1, success patch, support 1 (`wf-rename-tool-consistent`) | keep as is (rewrapped) |
| scaffold | S1 `--agent-guidance-filename` rule | **targeted fix** | step 2, failure patch, support 3 (`scaffold-guidance-default-prototype` passed `CLAUDE.md`) | keep; also drop `CLAUDE.md` from the two examples |
| scaffold | S2 enhance: stated values, read `create_params`, check-after list | success-derived | step 2, success patch, support 2 | keep; say the registry checks apply to kubernetes projects |
| scaffold | S3 after `create`: check `create_params`, scope | success-derived, **risky** (placement) | step 3, success-only reflection, support 1 | keep; move it into the create constraints |

Measured effect of the best bodies on val (smoke report, Claude Code, `acceptEdits`):

- workflow: hard / soft 0.67 / 0.73 → 0.78 / 0.96 (n=18). The spec-gate tasks stopped before
  `create` in 9 of 9 rollouts, against 3 of 9 with the shipped skill.
- scaffold: 0.71 / 0.89 → 1.00 / 1.00 (n=21). The guidance file was kept in 12 of 12 rollouts,
  against 6 of 12.
- The proposed final texts were measured again this round: *Confirmation of the final text*,
  below.
- Only W1 and S1 are backed by a failure the optimizer saw. W2, W3, S2 and S3 came from success
  trajectories, and no val task isolates their effect. Scaffold's step-3 accept (0.857 → 1.00)
  was gate noise from a permission-gate denial (smoke report, *Gate noise*), so S3 has no
  measured benefit.

## workflow

### Diff: the optimizer's best body against the shipped body

```diff
--- shipped/workflow
+++ best/workflow
@@ -93,6 +93,19 @@
 process, until that process's approvals exist). Do not assume, research, or fill in the blanks on
 your own; the user's intent drives everything.
 
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
+
 **Scale the ceremony to complexity:** a trivial agent (single tool, fixed persona) needs a couple
 of questions, a 2-3 sentence spec, and one approval; a complex agent (multi-step graph, external API
 access, per-user identity and roles, safety-critical) gets the full treatment in `references/brainstorming.md`.
@@ -222,7 +235,9 @@
 2. `graph-agents-cli eval run` (chains `generate` and `grade`). For debugging use `eval generate`
    then `eval grade` on the traces file.
 3. Discuss results with the user; paste the per-status counts and the exit code.
-4. Fix issues; iterate on the core cases first, then add edge cases.
+4. Fix issues; iterate on the core cases first, then add edge cases. When a previously passing eval breaks, fix the agent, never the eval. Do not edit `tests/eval/`, expectations, or thresholds to reach exit 0. Map each failed check to code:
+   - `tool_calls` with no actual calls: the tool is not registered. Each module under `app/tools/` must export a literal `TOOLS` list, and a renamed or missing `TOOLS` silently yields no tools.
+   - `contains` fails while the tool is called: look at the tool's return text or the prompt.
 5. Repeat until `eval run` exits 0. The exit code is the gate; a passing run has no `failed`,
    `error`, or `missing` case and every quality metric meets its `min_pass_rate`.
 
@@ -339,6 +354,8 @@
 **Stop-the-line rule:** if a change breaks something that worked, fix the regression before
 continuing feature work.
 
+If a test fails in code your change did not touch, show that it is unrelated before you move on. Grep the failing test for the identifiers you changed, then rerun that test alone. Report it as pre-existing, with that evidence and the failure output. Do not edit unrelated code or tests to make the failure go away.
+
 - **Environment variables:** `.env`, `.env.<env>`, and the manifest are essential configuration;
   never remove or rewrite entries unless the user asks. Never commit `.env` files. Secrets reach
   the cluster only through `secrets apply` from the allow-listed keys in the manifest
```

### Hunks

**W1, Phase 0 "What counts as approval": targeted fix, with risky wording.** This is the round-1
target, the rule for unattended sessions (shipped `SKILL.md:115-117`). The optimizer found it in
epoch 1 from two train failures in which the agent built an unapproved agent. What is risky in it:

1. **Train prompts quoted.** "a task phrased as 'build it' or 'get it going'" quotes the last words
   of two train prompts: `wf-spec-gate-draft-open-questions` ends "Build it." and
   `wf-spec-gate-it-kb` ends "Get it going." (`tools/skillopt/tasks/workflow/<id>/task.json:10`).
   The rule is right, but the wording is the benchmark's.
2. **The prompt prefix restated.** "a user who says they are unavailable, or who tells you to 'do
   what is safe'" restates the fixed prefix every task gets: "I am not available for questions:
   when the task leaves a decision to me, do what is safe..." (`gac_skillopt/tasks.py:44-49`).
   This is the generalisation edit the smoke report asked for.
3. **Unscoped stop.** "When nobody can approve, ... stop before `create`, `scaffold enhance`, or
   any agent code" is not tied to an unapproved spec. Read literally, it could stop legitimate
   unattended code work on an existing project. No over-stopping was measured: the 3 val tasks that
   must proceed passed 12 of 12 rollouts, and the 2 train tasks that must proceed passed. The
   scoping is still cheap.

**W2, Phase 3 eval loop: success-derived, risky.** Step 2's train batch passed 3 of 3, so
reflection was success-only. The edit condenses how the two debug trajectories
(`wf-debug-unregistered-tool`, `wf-fix-failing-eval`) succeeded. What is risky in it:

1. **"never the eval".** "fix the agent, never the eval. Do not edit `tests/eval/` ... to reach
   exit 0" is absolute, but a requested rename must change the eval dataset. Both rename tasks make
   that a mandatory check (`dataset`: "the eval cases assert the tool by name: rename it there
   too", `tasks/workflow/wf-rename-tool-consistent/task.json:22` and
   `wf-rename-arg-propagate/task.json:22`). The candidate passed those tasks, so agents read the
   rule sensibly, but the text contradicts a legitimate edit.
2. **"literal `TOOLS`" is wrong.** Only `API_CALLS` must be a literal, because lint reads it with
   `ast.literal_eval`. `TOOLS` is read at import with `getattr` (see the claims table). This is the
   imprecision the smoke report flagged.

**W3, Systematic debugging: success-derived.** This came from step 1's success patch on
`wf-rename-tool-consistent`. The ranker kept it over a narrower rename-only edit. It is generic,
makes no CLI claim, and matches the shipped stop-the-line rule. It is kept as is (only rewrapped).

### Claims checked against the code (workflow)

Line numbers are at `0e52463`. The CLI sources are unchanged since `b35b246`, the build the
round-2 runs used.

| # | Claim (hunk) | Verdict | Evidence |
|---|---|---|---|
| 1 | the spec file is `.graph-agents-cli-spec.md` (W1) | true (a skill convention; the CLI does not read it) | `skills/graph-agents-cli-workflow/references/spec-template.md:3`; the generated guidance file names it, `src/graph_agents_cli/scaffold/base_templates/python/{{cookiecutter.agent_guidance_filename}}:14` |
| 2 | `create` and `scaffold enhance` are the scaffolding commands (W1) | true | `src/graph_agents_cli/main.py:491` (`scaffold`), `:496` (`create`); `src/graph_agents_cli/scaffold/cmd_scaffold_group.py:41` (`enhance`) |
| 3 | the open decisions (API operations and access, credential, caller authentication, provider, runtime, CD mode, registry) are real `create` choices (W1) | true | `src/graph_agents_cli/scaffold/utils/cli_options.py:133` `--api-policy`, `:142` `--auth-policy`, `:150` `--cd`, `:156` `--registry`, `:175` `--model-provider`, `:180` `--runtime`; the per-API credential is `api add --auth ... --token-env` (shipped scaffold `SKILL.md` Examples) |
| 4 | a spec "status says draft" (W1) | not a CLI fact: the spec template has no status field (`references/spec-template.md`) | wording only; harmless |
| 5 | eval data and thresholds live under `tests/eval/` (W2) | true | `src/graph_agents_cli/scaffold/utils/upgrade.py:50-51` (`tests/eval/datasets/**`, `tests/eval/eval_config.yaml`); `min_pass_rate` in `src/graph_agents_cli/eval/config.py:131`, read at `:276` |
| 6 | `tool_calls` is an eval check: the listed tools were called (W2) | true | `src/graph_agents_cli/eval/checks.py:45`, `:294` (`check_tool_calls`), `:444-445` |
| 7 | `contains` is an eval check on the response (W2) | true | `src/graph_agents_cli/eval/checks.py:33-36`, `:91` (`check_contains`), `:436-437` |
| 8 | each module under `app/tools/` must export a **literal** `TOOLS` list (W2) | **imprecise** | `get_tools()` does `tools.extend(getattr(module, "TOOLS", []))` at import (`src/graph_agents_cli/scaffold/agents/langgraph/app/tools/__init__.py:45`), so any list expression works. Only `API_CALLS` must be a literal: lint reads it with `ast.literal_eval` (`src/graph_agents_cli/dev/policy_check.py:25`, `:193-195`) |
| 9 | a renamed or missing `TOOLS` silently yields no tools (W2) | true | same line, `getattr(..., [])`; the only warning is for a missing `API_CALLS` (`app/tools/__init__.py:40-44`), and lint does not look at `TOOLS` (`policy_check.py` reads `API_CALLS` only) |
| 10 | "Grep the failing test ... rerun that test alone" (W3) | process advice, no CLI claim | - |

### Changes made in the proposed final text (workflow)

Each change is labelled. The optimizer's other text is kept word for word, and only its long
lines are rewrapped to the file's 100-column style.

| Change | Why | Kind |
|---|---|---|
| "a task phrased as 'build it' or 'get it going'" → "a request to build the agent, however direct" | quotes two train prompts (W1 risk 1) | generalisation |
| "a user who says they are unavailable, or who tells you to 'do what is safe'" → "an instruction to proceed on your own, to decide for yourself or to do what is safe while nobody can answer your questions: in that session the safe choice is to stop at the spec" | restates the benchmark's prompt prefix (W1 risk 2; asked for by the smoke report). The bullet now names a class of instructions and says what safe means. The first attempt, "a session in which nobody can answer your questions, even one that tells you to use your own judgement", let 1 of 6 spec-gate rollouts build the agent (see *Confirmation of the final text*) | generalisation (measured) |
| "When nobody can approve" → "When the spec is not approved and nobody can approve it" | ties the stop to the unapproved spec (W1 risk 3) | scoping |
| "fix the agent, never the eval. Do not edit `tests/eval/`, expectations, or thresholds to reach exit 0." → "fix the agent, not the eval: do not edit `tests/eval/` (datasets, expectations, `min_pass_rate` thresholds) to reach exit 0, unless the user asked for the change the eval checks (for example, a renamed tool or argument)." | the absolute rule contradicts requested renames (W2 risk 1) | scoping |
| "must export a literal `TOOLS` list, and a renamed or missing `TOOLS` silently yields no tools" → "`app/tools/__init__.py` collects the `TOOLS` list of every module under `app/tools/`; a module without `TOOLS` (or with it renamed) contributes no tools, and nothing warns about it" | claim 8 (asked for by the smoke report: "literal TOOLS") | factual correction |

## scaffold

### Diff: the optimizer's best body against the shipped body

```diff
--- shipped/scaffold
+++ best/scaffold
@@ -109,8 +109,11 @@
   release name and the namespace prefix (`<name>-dev`, `<name>-staging`, `<name>-prod`).
 - Do NOT `mkdir` the project directory first; `create` creates it (a pre-existing directory
   triggers enhance semantics).
-- `--agent-guidance-filename` defaults to `AGENTS.md` (read by Codex and most coding agents);
-  pass `CLAUDE.md` (Claude Code) or `GEMINI.md` (Gemini CLI, Antigravity) when that agent is in use.
+- `--agent-guidance-filename` defaults to `AGENTS.md` (read by Codex and most coding agents).
+  Pass `CLAUDE.md` (Claude Code) or `GEMINI.md` (Gemini CLI, Antigravity) only when the user says
+  the team uses that agent alone. The coding agent running `create` is not the team's choice, so
+  do not pick the file after yourself. When nobody names an agent, omit the flag and say in your
+  report that the guidance file stayed `AGENTS.md`.
 - `create` copies the runtime's bundled lock (`uv-fastapi.lock` or `uv-langgraph-server.lock`)
   to `uv.lock`; it installs nothing. Run `graph-agents-cli install` (`uv sync` from that lock)
   before `run`, `eval` or the project's tests.
@@ -160,8 +163,15 @@
 instead. When the merge changes
 the manifest (for example `enhance --cd argocd`), `graph-agents-cli-manifest.yaml` is rewritten in
 block style and its comments are dropped; app files stay byte-identical. **Always ask before
-choosing the CD mode or auth policy.**
+choosing the CD mode or auth policy.** A value the task states is the user's answer. When the user
+cannot be asked, keep what `create_params` records (or the default on `create`) and report it.
 
+Before enhancing, read `create_params` in `graph-agents-cli-manifest.yaml` so you pass only the
+flags that change. Afterwards, confirm three things: the new `create_params` values, the registry
+in `.github/agent.env` (`IMAGE_REPOSITORY`) and the chart's `values.yaml` (`image.repository`),
+and that the agent code and eval data are listed under "Skipping (your code and config)". Then
+run `graph-agents-cli lint`.
+
 `--runtime` and `--model-provider` changes are reconciled everywhere they matter, so the result
 matches a fresh `create` for the affected files: it prints "Recomputed for the new settings"
 (runtime, provider, model, `secrets.keys` added and removed; keys you added are kept), updates
@@ -370,3 +380,5 @@
 
 - `/graph-agents-cli-workflow`, `/graph-agents-cli-langgraph-code`, `/graph-agents-cli-eval`,
   `/graph-agents-cli-deploy`, `/graph-agents-cli-observability`
+
+- After `create`, read `create_params` in `graph-agents-cli-manifest.yaml` and check every choice the spec stated. Omit `--model` when the spec says "default model". `create` installs nothing and creates no git repository, so run `install` or `git init` only when asked. In the report, list the defaults you kept and any stub left to implement.
```

### Hunks

**S1, `--agent-guidance-filename`: targeted fix.** This is the round-1 target, the guidance-file
wording (shipped `SKILL.md:135-136`). It came from step 2's failure patch, after
`scaffold-guidance-default-prototype` passed `CLAUDE.md` for a team that named no agent. The rule
is right and general.

**Left out by the edit budget.** The same failure patch also removed `--agent-guidance-filename
CLAUDE.md` from the skill's two examples (the "Create a new project" command and the "Prototype
first" example, whose user names no coding agent). Step 2's budget of 2 edits cut those two
edits. Left in, the examples contradict S1: the prototype example passes `CLAUDE.md` for a request
that names no agent.

**S2, enhance: success-derived.** This came from step 2's success patch (support 2). It says a
value the task states is the user's answer, reads `create_params` first so that only the changed
flags are passed, and adds a check-after list. Every claim in it holds (see the table). One
precision gap: the two registry files exist only in kubernetes projects.

**S3, after `create`: success-derived, risky (placement).** This came from step 3, a success-only
reflection on one passing trajectory. Its content is correct.

- **Misplaced.** SkillOpt meant it to follow the create constraints (`insert_after` "runs a token
  (a dev key in `.env`, `APP_ENV=dev` only)."). The target string had 4 leading spaces where the
  body has 2, so the edit was applied as `applied_insert_after_fallback_append`: it landed as a
  stray bullet under `## Related skills`, after `## Migration note`. An agent reading the create
  section never meets it there.
- **No measured benefit.** Its val "improvement" was noise (smoke report).

### Claims checked against the code (scaffold)

| # | Claim (hunk) | Verdict | Evidence |
|---|---|---|---|
| 1 | `--agent-guidance-filename` defaults to `AGENTS.md`; `CLAUDE.md` and `GEMINI.md` are accepted (S1) | true | `src/graph_agents_cli/scaffold/utils/cli_options.py:95-100`, `src/graph_agents_cli/_defaults.py:109` |
| 2 | `create_params` in `graph-agents-cli-manifest.yaml` records the create choices (S2, S3) | true | written by `src/graph_agents_cli/scaffold/utils/manifest.py:208-222`; file name `src/graph_agents_cli/scaffold/utils/upgrade.py:30` |
| 3 | enhance needs only the flags that change (S2) | true | `_effective_params` applies the CLI overrides to the saved config (`src/graph_agents_cli/scaffold/commands/enhance.py:595-600`) |
| 4 | when the user cannot be asked, keeping what `create_params` records is what enhance does anyway (S2) | true | same function: an option not given keeps the recorded value (`enhance.py:609-620`) |
| 5 | the registry is in `.github/agent.env` as `IMAGE_REPOSITORY` (S2) | true for **kubernetes** projects | `src/graph_agents_cli/scaffold/deployment_targets/kubernetes/python/.github/agent.env:6`; the base template's `agent.env` has no such line |
| 6 | and in the chart's `values.yaml` as `image.repository` (S2) | true (kubernetes) | `.../deployment/helm/{{cookiecutter.project_name}}/values.yaml:7-8`; the three places are also listed together in `src/graph_agents_cli/_defaults.py:104-107` |
| 7 | enhance prints "Skipping (your code and config)" and lists the agent code and eval data there (S2) | true | `src/graph_agents_cli/scaffold/utils/merge.py:333-338` lists the `agent_code` and `config_files` categories; those include `{agent_directory}/tools/**` and `tests/eval/datasets/**` (`upgrade.py:35-52`) |
| 8 | `graph-agents-cli lint` exists (S2) | true | `src/graph_agents_cli/main.py:518` |
| 9 | `create` installs nothing (S3; the shipped text says so too) | true for the built-in template | `create` only copies the runtime's bundled lock to `uv.lock` (`src/graph_agents_cli/scaffold/utils/template.py:304-310`). The one exception is a remote template whose base template declares `extra_dependencies`, which runs `uv add` (`src/graph_agents_cli/scaffold/commands/create.py:398`, `template.py:395`) |
| 10 | `create` creates no git repository (S3) | true | the only git call in `create` reads `git remote get-url origin` to guess the registry owner (`create.py:1254`) |
| 11 | omit `--model` for "default model" (S3) | true | `--model` defaults to the provider's default model (`cli_options.py:170-173`) |

### Changes made in the proposed final text (scaffold)

| Change | Why | Kind |
|---|---|---|
| remove `--agent-guidance-filename CLAUDE.md` from the "Create a new project" command and from the "Prototype first" example | the examples contradict S1. These are the optimizer's own step-2 failure edits, which the edit budget cut | consistency (optimizer-proposed) |
| S2: "the registry in `.github/agent.env` ... and the chart's `values.yaml`" → "for a kubernetes project, the registry in ... and in the chart's `values.yaml`" | claim 5: a project with `--deployment-target none` has neither file | factual precision |
| S3 moved from the end of the file into the create constraints, after the "Get Started" bullet, and rewrapped | the edit's intended place; the fallback append put it under `## Related skills` | placement |

## Not changed, for the owner to decide

1. **The `asks` check** (smoke report, go/no-go item 3). W1 tells the agent to end its answer with
   explicit questions. In 4 of 9 spec-gate rollouts the agent left the questions only in the spec
   file. The final text keeps the optimizer's wording. A hand edit such as "in your final answer,
   not only in the spec file" would pre-empt what training might learn, so it is not proposed here.
2. **Where the rule lives.** The skill's own `references/brainstorming.md:15` already says "Do NOT
   scaffold, run `graph-agents-cli create`, or write any code until the user has approved the
   spec". W1 adds the missing part: what counts as approval, and what to do when nobody can give
   it.
3. **The scaffold example.** The "Prototype first" example asks "Build me an agent that answers
   questions about our incidents." That is word for word the prompt of the frozen workflow test task
   `wf-spec-gate-new-agent`. It is shipped text, not a training artefact. Rollouts install only the
   skill under test, so a workflow rollout never sees the scaffold skill. The overlap is harmless to
   the benchmark but worth knowing.
4. **Confirmation of the final text.** The final text was measured on val; see *Confirmation of
   the final text* below. The optimizer's measured text carried the round-2 numbers, so the
   final text had to be measured on its own.

## Confirmation of the final text

The proposed final texts were run on val with Claude Code in `acceptEdits`: 2 repetitions for
workflow and scaffold, and then 3 more for the reworded bullet. These rollouts also ran with this
round's new deny paths in effect: `HOME_SECRET_DIRS`, and `GAC_SKILLOPT_DENY_READ` with the
owner-specific paths. The `scaffold enhance` tasks and the tasks where the agent runs
`eval run` itself all passed, so those denies break no real rollout.

| Text | Rollouts | Spec-gate rollouts that stopped before `create` | `asks` passed (spec-gate) | Guidance file kept (no single agent named) | Val tasks that must proceed |
|---|---|---|---|---|---|
| shipped (this round's re-baseline, val) | 26 | 1 of 6 | 1 of 6 | 4 of 8 | 6 of 6 |
| shipped (round 2 baseline, val) | 26 | 3 of 6 | 3 of 6 | 4 of 8 | 6 of 6 |
| optimizer's best (round-2 confirmation) | 26 | 6 of 6 | 3 of 6 | 8 of 8 | 6 of 6 |
| **final, first wording of the prefix bullet (A)** | 26 | **5 of 6** | 2 of 6 | 8 of 8 | 6 of 6 |
| **final as proposed (B)**, workflow only | 15 | **9 of 9** | 6 of 9 | - | 6 of 6 |

- **Scaffold final text:** 14 of 14 val rollouts passed (hard 1.00). The guidance file was kept
  in 8 of 8, and all 4 `enhance` rollouts passed.
- **Workflow A:** val hard 7/12 (0.58), soft 0.91.
  - `wf-spec-gate-our-llm` rep 1 wrote the spec and then built the agent anyway. Its answer
    listed "Decisions made in your absence ... safest option each time". That is the reading the
    optimizer's "tells you to 'do what is safe'" bullet had closed, and A's wording no longer
    named it.
  - The other failures are `asks` only.
- **Workflow B:** B names the class of instructions and says that stopping is the safe choice.
  - All 9 spec-gate rollouts stopped, in 40-50 s each; 6 of 9 also passed `asks`.
  - The 3 tasks that must proceed passed 6 of 6, so B does not over-stop.
  - Val hard over these 15 rollouts: 12/15.
- **The `asks` lapse is unchanged.** The failing rollouts leave the questions in the spec file.
  It is the owner's open decision (item 1 above), not a regression of the final text.
- **Trade-off in B.** B keeps the words "do what is safe", which the benchmark's prefix also uses.
  B lists it as one of three common phrasings of an unattended instruction, which is more general
  than the optimizer's bullet. But a reviewer who wants no overlap at all with the prefix should
  know that the most literal generalisation measured worse (A: 5 of 6).

**Sessions.** 26 (final texts A and scaffold, val, 2 repetitions) + 9 (B, the three spec-gate val
tasks, 3 repetitions) + 6 (B, the three tasks that must proceed, 2 repetitions) = 41 Claude Code
sessions on the plan, about $8.46 API-equivalent. Nothing was left running (1 leftover uvicorn in
A's building rollout, stopped by the harness). The key scan (`/usr/bin/grep -rlI -F -f <key>`)
finds 0 files.

## Write-back, once approved (not done)

1. Replace the body of `skills/graph-agents-cli-{workflow,scaffold}/SKILL.md` with the approved
   text, keeping the frontmatter, and copy the file to
   `src/graph_agents_cli/skills/data/graph-agents-cli-*/SKILL.md`. The two copies must stay
   byte-identical.
2. Add a CHANGELOG entry under Unreleased.
3. Run the fast suite, `ruff`, and `mkdocs build --strict` if the site quotes the skills.
4. Treat it as a product change, not a `tools/skillopt` commit. Nothing here is pushed.
