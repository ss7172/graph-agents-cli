# Final skills check for 0.3: the shipped 0.2.0 skills against the 0.3 skills

This is round 3e, P7 scope 3: a full before/after run of gac-bench on Claude Code and Codex.
It ran on 2026-09-29.

- **Before:** the six skills as shipped in the `v0.2.0` tag.
- **After:** the six skills of branch `v0.3` at `d4f9734`. They hold every SkillOpt write-back:
  - the workflow text, with generic examples;
  - the scaffold guidance-file wording;
  - the observability salt procedure and its trace-header rule;
  - the "agents calling agents" and structured-answer guidance.

Both arms ran the same CLI build, the same harness code and the same harness versions. Only the
skill documents differed. Every rollout is in [`final-v0.3.json`](final-v0.3.json).

## Result

**Claude Code: better overall, with one real regression (1 of 2 reps of a new v0.3 deploy
task).** Over all 104
tasks, hard went from 0.84 to 0.97 (+0.12, 95% CI [+0.05, +0.19], p = 0.0006). On val+test it
went from 0.82 to 0.98 (+0.16 [+0.07, +0.25], p = 0.001). 17 tasks moved up and 2 moved down.

- **Where the gain is:** the two skills SkillOpt trained on.
  - workflow: val+test 0.36 → 1.00;
  - scaffold: val+test 0.61 → 1.00.
- **The rest is flat:** eval, deploy, langgraph-code and observability were each 0.92-0.96 or
  higher before, and moved by -0.00 to +0.06.
- **The 2 down moves:**
  - one is a train rollout that the permission gate blocked;
  - the other is one of two reps of a new v0.3 deploy task, where the agent added a restriction
    nobody asked for.

  See "Per-task flips".

**Codex (gpt-5.6-terra): the same direction on the part the budget covered.** It ran the test
split of all six skills plus val of observability and workflow: 30 tasks, 1 rep per arm. Hard
went from 0.77 to 0.97 (+0.20 [+0.07, +0.33], p = 0.031). 6 tasks moved up, none moved down.
The gain is again mostly workflow (0.43 → 1.00), plus the observability salt task
`obs-salt-loyalty-cards` (0 → 1) and the new `deploy-system-add-edge-retail` (0 → 1).

**The 18 new v0.3-feature tasks show no difference between the arms** (Claude 0.93 vs 0.93).
The 0.2 skills did not fail those tasks "by construction". Both arms run the same v0.3 CLI, so
the likely explanation is that its `--help` and lint messages carry most of what those tasks
need; this run did not test that. Pooling them with the legacy tasks dilutes the delta, so both
views are below.

**Test split (unseen by any training):**
- Claude: 0.83 → 0.97 (+0.14 [0.00, +0.31]);
- Codex: 0.83 → 0.94 (+0.11 [0.00, +0.28]).

Only 3 and 2 test tasks moved, all of them up. So the direction is clear, but neither test
delta is significant on its own (p = 0.25 and 0.5).

## Conditions

| | Claude Code | Codex |
|---|---|---|
| Harness | Claude Code **2.1.284**. `GAC_SKILLOPT_CLAUDE_BIN` was pinned to the versioned binary, so an update of the `claude` symlink during the run could not split the arms. | codex-cli **0.154.0** |
| Model | `sonnet` resolves to **claude-sonnet-5-5** (the preflight's init event). `--model claude-sonnet-5-5` was passed to both arms, and every rollout's init event reports it. | **gpt-5.6-terra** |
| Effort, turns | medium, 80 | medium, 80 |
| Permission mode | `acceptEdits`, Bash sandbox on (as since round 1) | the Codex permission profile of DESIGN section 3 |
| Tasks, reps | All 104. val and test: 2 reps each; train: 1 rep. 165 rollouts per arm. | test, all skills (18 tasks), plus val of observability and workflow (12 tasks); 1 rep. 30 rollouts per arm. |
| Slots, ports | 5 per arm, run concurrently. before 22400-22404 / 22425-22429; after 22450-22454 / 22475-22479. | 3 per arm, concurrently. before 22150-22152 / 22175-22177; after 22162-22164 / 22187-22189. |

- **CLI.** `graph-agents-cli, version 0.2.0+gd4f9734`: one scratch build from `v0.3` at
  `d4f9734`, shared by both arms. The version string still reads 0.2.0 because the release bump
  has not happened yet.
- **Arms.** There were two detached worktrees of `d4f9734`:
  - the before worktree's `skills/` was replaced by `git archive v0.2.0 skills`, the whole
    directories (frontmatter, body and `references/`); `git diff v0.2.0 -- skills/` is empty;
  - the after worktree is untouched.

  `--body-dir` was not used: it keeps the 0.3 frontmatter and references. The SKILL.md hashes
  (sha256, first 16 hex characters) recorded in each run's `summary.json`:

  | Skill | before (v0.2.0) | after (d4f9734) |
  |---|---|---|
  | workflow | 79049d4e5ef1869f | 92e7a93903b44c46 |
  | scaffold | 69ac60ef59a84d41 | 7e4913640d5a534a |
  | langgraph-code | a4deab4607c050f8 | 684f8aeab95544e3 |
  | eval | c1392683969e9bbc | 47ebbd777af86ecf |
  | deploy | 1c744b7e804c708b | ea0f2972a4da1dcb |
  | observability | af66b46cb02f8668 | 30e6f3248c96f9e4 |

- **Isolation.** The isolation of DESIGN section 3 applied. The preflight passed in this
  configuration (`preflight: ok`). Every gating check was ok, and as in rounds 3c and 3d the
  non-gating `other_run_workspace_unreadable` failed. The session loaded only the skill under
  test plus the built-in `agents-md` plugin, with no MCP servers and no hook events.
  - **Extra read denials:** `GAC_SKILLOPT_DENY_READ` denied both arm worktrees and every other
    scratch area of the session.
  - **Cross-arm audit.** The arms ran at the same time, so an agent could in principle read the
    other arm's workspace. An audit of every trace looked for three things: another run's
    workspace path, an absolute path to a `SKILL.md` or skills directory outside the rollout's
    own, and a filesystem-wide search for skills. It found **0** such reads in 390 rollouts.
- **Statistics.**
  - Hard and soft are means over rollouts.
  - CIs are 95% bootstrap intervals that resample **tasks** (10,000 resamples), because
    rollouts of one task are not independent.
  - A delta is the mean over tasks of (after task mean - before task mean), with a paired
    bootstrap over tasks.
  - p is a two-sided sign-flip permutation test over the task deltas, exact up to 16 non-zero
    deltas.
  - Most tasks score the same in both arms, so per-skill p-values are large even where every
    moved task moved up.
- **Splits.**
  - train was optimised on and val was SkillOpt's selection gate, for workflow, scaffold and
    observability;
  - test is frozen and unseen;
  - eval, deploy and langgraph-code were never trained, so all three of their splits are
    unseen for them.
- **Not comparable with earlier rounds' absolute numbers.** Rounds 3c and 3d pinned Claude Code
  2.1.283 and `claude-sonnet-5`. This run is the re-baseline that ROUND3D asked for, on the
  current harness and model.

## Claude Code (165 rollouts per arm)

Hard and soft per group: before, after, and the paired delta. "legacy" is the 86 tasks that
existed before 0.3; "new-v0.3" is the 18 tasks in the six v0.3 families.

| Group | Tasks | hard before | hard after | hard delta | soft before | soft after | soft delta |
|---|---|---|---|---|---|---|---|
| **overall, all** | 104 | 0.84 [0.77, 0.90] | **0.97** [0.94, 0.99] | **+0.12** [+0.05, +0.19], p=0.0006 | 0.94 [0.91, 0.96] | 0.98 [0.97, 0.99] | +0.04 [+0.02, +0.06], p=0.001 |
| overall, val+test | 61 | 0.82 [0.73, 0.90] | 0.98 [0.94, 1.00] | +0.16 [+0.07, +0.25], p=0.001 | 0.93 [0.90, 0.96] | 0.98 [0.97, 0.99] | +0.05 [+0.02, +0.08], p=0.0009 |
| overall, test | 18 | 0.83 [0.67, 0.97] | 0.97 [0.92, 1.00] | +0.14 [0.00, +0.31], p=0.25 | 0.95 [0.90, 0.99] | 0.98 [0.95, 1.00] | +0.04 [0.00, +0.07], p=0.12 |
| overall, val | 43 | 0.81 [0.71, 0.91] | 0.98 [0.94, 1.00] | +0.16 [+0.07, +0.27], p=0.007 | 0.92 [0.88, 0.96] | 0.98 [0.96, 1.00] | +0.06 [+0.02, +0.10], p=0.004 |
| overall, train | 43 | 0.88 [0.79, 0.98] | 0.95 [0.88, 1.00] | +0.07 [-0.02, +0.16], p=0.38 | 0.96 [0.91, 0.99] | 0.98 [0.95, 0.99] | +0.02 [-0.01, +0.06], p=0.34 |
| legacy, val+test | 49 | 0.80 [0.69, 0.89] | 0.99 [0.97, 1.00] | +0.19 [+0.09, +0.30], p=0.0005 | 0.93 [0.89, 0.96] | 0.99 [0.98, 1.00] | +0.06 [+0.03, +0.10], p=0.0001 |
| legacy, test | 12 | 0.79 [0.58, 1.00] | 1.00 | +0.21 [0.00, +0.42], p=0.25 | 0.95 [0.90, 1.00] | 1.00 | +0.05 [0.00, +0.10], p=0.25 |
| new-v0.3, all | 18 | 0.93 [0.84, 1.00] | 0.93 [0.84, 1.00] | +0.00 [-0.08, +0.08], p=1 | 0.94 [0.88, 0.99] | 0.95 [0.91, 0.99] | +0.00 [-0.05, +0.06] |
| new-v0.3, val+test | 12 | 0.92 [0.79, 1.00] | 0.92 [0.79, 1.00] | +0.00 [-0.12, +0.12], p=1 | 0.93 [0.85, 0.99] | 0.95 [0.90, 0.99] | +0.02 [-0.05, +0.10] |

### Per skill (hard)

| Skill | val+test before | val+test after | delta | test before → after | train before → after |
|---|---|---|---|---|---|
| workflow | 0.36 [0.07, 0.71] | **1.00** | **+0.64** [+0.29, +0.93], p=0.06 (5 up, 0 down) | 0.00 → 1.00 (1 task) | 0.67 → 1.00 |
| scaffold | 0.61 [0.28, 0.89] | **1.00** | **+0.39** [+0.11, +0.72], p=0.12 (4 up, 0 down) | 0.50 → 1.00 | 0.71 → 1.00 |
| langgraph-code | 0.92 [0.83, 1.00] | 0.94 [0.86, 1.00] | +0.03 [0.00, +0.08] | 0.93 → 0.93 | 0.91 → 0.91 |
| eval | 0.94 [0.83, 1.00] | 1.00 | +0.06 [0.00, +0.17] | 0.83 → 1.00 | 1.00 → 1.00 |
| deploy | 0.95 [0.85, 1.00] | 0.95 [0.85, 1.00] | +0.00 [-0.15, +0.15] (1 up, 1 down) | 1.00 → 1.00 | 1.00 → 1.00 |
| observability | 0.94 [0.81, 1.00] | 1.00 | +0.06 [0.00, +0.19] | 1.00 → 1.00 | 1.00 → 0.83 (gate-blocked, below) |

Soft follows the same pattern, for example workflow 0.88 → 1.00, scaffold 0.84 → 1.00 and
observability val+test 0.90 → 0.96. The before arm had 17 workflow and scaffold rollouts that
failed for a reason other than the permission gate. All 17 failed on exactly the checks
SkillOpt targeted:
- `asks`: the spec-gate stop that must end with questions;
- `guidance`: the default guidance file when the spec names no coding agent;
- scaffold's `create --process` guidance.

**Per-session cost** (API-equivalent on the plan):
- before: $20.43 over 165 sessions, 8.9 turns and 41.6 s of agent time per rollout;
- after: $19.63, 8.4 turns and 33.4 s.

The 0.3 texts are longer but did not make sessions longer.

## Codex, gpt-5.6-terra (30 rollouts per arm)

| Group | Tasks | hard before | hard after | hard delta | soft before | soft after | soft delta |
|---|---|---|---|---|---|---|---|
| **overall** (test + val of observability, workflow) | 30 | 0.77 [0.60, 0.90] | **0.97** [0.90, 1.00] | **+0.20** [+0.07, +0.33], p=0.031 (6 up, 0 down) | 0.91 [0.87, 0.95] | 0.96 [0.92, 0.98] | +0.04 [+0.01, +0.08], p=0.031 |
| test, all six skills | 18 | 0.83 [0.67, 1.00] | 0.94 [0.83, 1.00] | +0.11 [0.00, +0.28], p=0.5 | 0.95 [0.89, 0.99] | 0.98 [0.94, 1.00] | +0.03 [-0.01, +0.09] |
| val, observability + workflow | 12 | 0.67 [0.42, 0.92] | 1.00 | +0.33 [+0.08, +0.58], p=0.12 | 0.87 [0.79, 0.94] | 0.93 [0.86, 0.98] | +0.06 [+0.01, +0.11] |
| legacy (val+test run) | 24 | 0.79 [0.62, 0.96] | 1.00 | +0.21 [+0.04, +0.38], p=0.06 | 0.92 [0.87, 0.96] | 0.96 [0.93, 0.99] | +0.05 [+0.02, +0.08] |
| new-v0.3 (test) | 6 | 0.67 [0.33, 1.00] | 0.83 [0.50, 1.00] | +0.17 [0.00, +0.50] | 0.90 [0.79, 1.00] | 0.93 [0.85, 1.00] | +0.03 |

| Skill (tasks run) | hard before → after | soft before → after |
|---|---|---|
| workflow (val 6 + test 1) | 0.43 → **1.00** (4 up) | 0.86 → 0.97 |
| observability (val 6 + test 2) | 0.88 → 1.00 (1 up: `obs-salt-loyalty-cards`) | 0.87 → 0.91 |
| deploy (test 3) | 0.67 → 1.00 (1 up) | 0.89 → 1.00 |
| langgraph-code (test 7) | 0.86 → 0.86 | 0.96 → 0.94 |
| eval (test 3) | 1.00 → 1.00 | 1.00 → 1.00 |
| scaffold (test 2) | 1.00 → 1.00 | 1.00 → 1.00 |

**Not run on Codex (budget).** Codex cost $0.20 per rollout on average, more than the $0.12-0.15
of earlier rounds. The v0.3 langgraph-code and deploy tasks cost $0.25-0.50 each. So $12 did not
cover val+test for both arms, which is 122 rollouts, about $24. Two parts were left out:
- val of scaffold, eval, deploy and langgraph-code (31 tasks);
- the planned second rep on workflow, scaffold and observability.

The test split covers all six skills, so the omission is val-only. The priority order was: test
first, since it is unseen, then the val of the two skills with Codex-specific findings
(observability's salt procedure and workflow's over-stop). Each chunk ran both arms together, so
every Codex task that ran has both arms.

## Per-task flips

Tasks whose hard score changed between the arms, as the mean over reps; rows are rollouts in
rep order.

**Claude Code: 17 up, 2 down**

| Task | Skill | Split | before | after | Why |
|---|---|---|---|---|---|
| `wf-spec-gate-new-agent` | workflow | test | 0 0 | 1 1 | `asks`: the 0.2 text stops without questions |
| `wf-spec-gate-orders-openapi` | workflow | val | 0 0 | 1 1 | `asks` |
| `wf-spec-gate-our-llm` | workflow | val | 0 0 | 1 1 | `asks` |
| `wf-spec-gate-slack-digest` | workflow | val | 0 0 | 1 1 | `asks` |
| `wf-spec-gate-it-kb` | workflow | train | 0 | 1 | `asks` |
| `wf-spec-gate-prototype-please` | workflow | train | 0 | 1 | `asks` |
| `wf-rename-arg-propagate` | workflow | val | 1 0 | 1 1 | before rep 2 blocked by the permission gate (benchmark) |
| `scaffold-create-process` | scaffold | test | 0 0 | 1 1 | `guidance`: no `AGENTS.md` under `create --process` |
| `scaffold-guidance-default-lgs` | scaffold | val | 0 0 | 1 1 | `guidance`: the default guidance file |
| `scaffold-guidance-unstated` | scaffold | val | 0 0 | 1 1 | `guidance` |
| `scaffold-guidance-default-prototype` | scaffold | train | 0 | 1 | `guidance` |
| `scaffold-enhance-registry` | scaffold | val | 1 0 | 1 1 | before rep 2 blocked by the permission gate (benchmark) |
| `scaffold-create-onprem-vllm` | scaffold | train | 0 | 1 | before blocked by the permission gate (benchmark) |
| `eval-honest-report` | eval | test | 0 1 | 1 1 | before rep 1 blocked by the permission gate (benchmark) |
| `obs-salt-local-dev` | observability | val | 0 1 | 1 1 | before rep 1 blocked by the permission gate (benchmark) |
| `deploy-ingress-staging` | deploy | val | 1 0 | 1 1 | before rep 2 never ran `deploy --dry-run` (agent) |
| `code-answer-invoice-fields` | langgraph-code | val | 1 0 | 1 1 | before rep 2 obeyed the template `AGENTS.md` spec gate and stopped (below) |
| **`deploy-system-file-travel`** | deploy | val | 1 1 | **0** 1 | **down** (new v0.3 task): after rep 1 also set `approvals: deny` on the flights edge, which the request did not ask for. The verifier wants only the hotels edge denied. Real, 1 of 2 reps. |
| **`obs-salt-rotate-staging`** | observability | train | 1 | **0** | **down**: the permission gate denied both commands that would write the new salt, and the agent stopped and said so (benchmark) |

**Codex: 6 up, 0 down**

| Task | Skill | Split | before | after | Why |
|---|---|---|---|---|---|
| `wf-spec-gate-new-agent` | workflow | test | 0 | 1 | `asks` |
| `wf-spec-gate-orders-openapi`, `-our-llm`, `-slack-digest` | workflow | val | 0 | 1 | `asks` |
| `obs-salt-loyalty-cards` | observability | val | 0 | 1 | `names-apply`: the salt reaches the cluster only through `secrets apply` (the salt procedure) |
| `deploy-system-add-edge-retail` | deploy | test | 0 | 1 | the system file is the spec: the new edge goes in `graph-agents-system.yaml`, then `system apply` |

**Regressions.**
- **Claude:** one real one, `deploy-system-file-travel` (1 of 2 val reps). The train-split
  "regression" is the permission gate.
- **Codex:** none.
- **Test split:** no test task's mean went down on either harness. One rollout did score 0 where
  the other arm's same rep scored 1: `code-peer-inventory-audience` after rep 1. Its before rep 2
  also scored 0, so the task mean is 0.5 in both arms.

## Failures caused by the benchmark, not the skill

- **Permission gate (`acceptEdits`, Claude only).** 81 before and 80 after rollouts had at least
  one command denied. That is the same rate in both arms: compound or `$(...)` shell commands
  the gate cannot approve without a prompt.
  - In **6 rollouts the denial blocked the task**, and the agent stopped and said so: 5 before
    and 1 after (listed in the flips table).
  - **Excluding them** (and the next item) gives Claude:
    - overall hard 0.87 → 0.98 (+0.10 [+0.05, +0.17], p = 0.001);
    - val+test 0.85 → 0.98 (+0.12 [+0.04, +0.21], p = 0.008);
    - legacy val+test 0.83 → 0.99 (+0.15 [+0.06, +0.26]);
    - observability train 1.00 → 1.00.

    The conclusion is unchanged.
  - The imbalance (5 against 1) may be partly a skill effect: the 0.3 workflow text changes how
    an unattended session proceeds. It is reported as a benchmark confound, as in every round
    since round 1.
- **`code-temperature-tool` (train): the verifier is too strict.** Its `others-untouched` check
  forbids any change under `tests/`. Both arms added a unit test for the new tool, which the
  langgraph-code skill recommends, and both failed. It is a benchmark defect: `tests/` should
  allow new files. It is excluded in the sensitivity numbers above.
- **`code-rpc-allow-inventory` (test, Codex): the verifier is borderline.** It failed in both
  Codex arms on different checks. Claude passed it in all 4 rollouts.
  - The after arm wrote the denial as `{rpc_method: item.delete, methods: [POST]}`. On a JSON-RPC
    API every call is a POST, so it behaves the same as the rule the verifier wants
    (`api deny --rpc-method item.delete`, with no methods). It is still scored as a failure.
  - The before arm failed `only-listed`: its allow entries were not exactly the two
    `rpc_method` entries at POST /rpc. That is a real error.
  - Separately, no 0.3 skill teaches `api allow/deny --rpc-method` (below).
- **Template `AGENTS.md` spec gate (both arms).** In 1 Claude rollout per arm, the agent read
  the generated project's `AGENTS.md` ("`process: null`: a written spec must be approved before
  any code") and stopped with a draft spec instead of making a concrete change:
  - `code-system-prompt-preserve`: after rep 1, where the skill was not loaded; before rep 1;
  - `code-answer-invoice-fields`: before rep 2.

  Both arms use the same template, so this does not bias the comparison. It is a product
  question for the template text (see "Issues").
- **Other benchmark signals.**
  - Skill not loaded: before 1, after 3. Two of the three after-arm cases are
    `deploy-system-add-edge-retail`, which passed anyway.
  - 0 timeouts, 0 infrastructure errors, 0 retried sessions.
  - 4 Codex rollouts left a process running; the harness stopped it and none was scored down.
  - 0 cross-arm reads.
- **Failures the agent owns** (they appear in both arms, so they are not skill effects):
  - `code-peer-inventory-audience` (test): 1 of 2 reps per arm; the peer entry fails the
    `target` (audience), `calls` and `relay-gate` checks;
  - `wf-rename-arg-propagate`: covered above;
  - `code-system-prompt-preserve`: covered above.

## Spend and sessions

- **Claude Code** (owner's plan; no ledger):
  - 330 benchmark rollouts;
  - 4 smoke rollouts (two tasks per arm, to check provenance and isolation);
  - 1 preflight session;
  - **335 sessions** in total.
- **Codex** (OpenAI key, track `skillopt`):
  - 60 rollouts, priced at **$11.34**. The ledger holds $11.36 under agent `gac-bench`, because
    each entry is rounded up.
  - One key probe (gpt-5-mini, $0.0001, agent `p7-bench-B`).
  - Prices: gpt-5.6-terra $2.00 input / $0.20 cached / $12.00 output / $2.50 cache writes per
    1M tokens, read on 2026-09-28.
  - Track `skillopt`: $64.57 → **$75.93** of $90. This step's cap was $12.

## Issues

| Severity | Category | Issue | Evidence | Where |
|---|---|---|---|---|
| minor | skills | No 0.3 skill teaches `api allow/deny --rpc-method` for JSON-RPC APIs. The `code-jsonrpc-policy` family depends on `--help`. | `grep -rn rpc-method skills/` finds nothing; Codex failed `code-rpc-allow-inventory` in both arms. | skills/graph-agents-cli-langgraph-code/SKILL.md (the API section, around line 502) |
| minor | benchmark | `code-temperature-tool`'s `others-untouched` forbids new files under `tests/`, so an agent that adds the unit test the skill asks for fails. | Both arms fail only that check (`tests/unit/test_temperature*.py (added)`). | tools/skillopt/tasks/langgraph-code/code-temperature-tool/task.json (`others-untouched.paths`) |
| minor | benchmark | `code-rpc-allow-inventory`'s `delete-denied` refuses `{rpc_method: item.delete, methods: [POST]}`, which behaves the same on a JSON-RPC API. | Codex after-arm rollout. | tools/skillopt/tasks/langgraph-code/code-rpc-allow-inventory/task.json |
| minor | template | The generated `AGENTS.md` (`process: null`: spec before code) makes unattended agents stop on concrete change requests even with the langgraph-code skill loaded. The workflow skill's scope rule does not reach sessions that load only another skill. | 3 of 330 Claude rollouts, in both arms. | the template's `AGENTS.md` |
| minor | skills | New deploy task `deploy-system-file-travel`: 1 of 2 after-arm reps put `approvals: deny` on an edge the request did not name. | Claude after rep 1. | skills/graph-agents-cli-deploy/SKILL.md (`system apply` edges) |
| minor | benchmark | The `acceptEdits` permission gate blocks compound commands, and blocked 6 of 330 Claude rollouts (5 before, 1 after). It is a known confound since round 1; `bypassPermissions` failed its preflight in round 3a. | See "Failures caused by the benchmark". | tools/skillopt/gac_skillopt/isolation.py (CLAUDE_PERMISSION_MODE) |
| note | provenance | v0.3 HEAD moved during the run: 159262b edited 2 lines of the deploy SKILL.md (client ids became actor ids). The measured after text is the `d4f9734` one (`ea0f2972…`); the difference is wording in one paragraph and one troubleshooting row. | `git diff d4f9734 159262b -- skills/` | skills/graph-agents-cli-deploy/SKILL.md |
| note | provenance | The 0.3.0 release commit then changed every `SKILL.md`'s `metadata.version`, its install pins (`@v0.2.0` to `@v0.3.0`) and the workflow skill's `Requires` line, so the shipped hashes differ from the table above in all six skills; no rule or instruction changed. | `git diff a7edef4 <release commit> -- skills/` | skills/*/SKILL.md |

Carried over from round 3d (context, not new):
- the shipped workflow text is not the text S1 measured, because the agents-calling-agents
  section came after S1. This run measures the shipped one;
- the observability body is 1.283 x the 0.2.0 body, over gac-bench's 1.25 x growth cap.

## Reproducing

From a scratch outside the checkout (see the README for `setup` and `preflight`):

```bash
git worktree add --detach $W/after  d4f9734
git worktree add --detach $W/before d4f9734
(cd $W/before && rm -rf skills && git archive v0.2.0 skills | tar -x)
export GAC_SKILLOPT_CLAUDE_BIN=<the versioned claude binary>
export GAC_SKILLOPT_DENY_READ=$W/before:$W/after:<other scratch areas>
# per arm, both arms at once:
(cd $W/<arm>/tools/skillopt && python -m gac_skillopt.baseline run --harness claude \
   --model claude-sonnet-5-5 --splits val,test --reps 2 --slots 5 --port-base <22400|22450> \
   --out $S/runs/claude-vt/<arm>)
# ... then --splits train --reps 1 (--out claude-train/<arm>)
# Codex: --harness codex --model gpt-5.6-terra --splits test (then val --skill observability
#   --skill workflow) --reps 1 --slots 3 --port-base <22150|22162> --estimate 0.20
```

The comparison (task-cluster bootstrap, sign-flip test, flips and the cross-arm trace audit) is
a scratch script. Its output is `final-v0.3.json`:
- `results.<harness>.groups`: aggregates and paired deltas;
- `.tasks`: per task, the hard and soft of each rep and arm;
- `.rollouts`: one compact row per rollout;
- `.benchmark_side`: the rollouts flagged above;
- `sensitivity_groups`: the aggregates without the gate-blocked rollouts and
  `code-temperature-tool`.
