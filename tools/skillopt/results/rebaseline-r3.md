# gac-bench round 3a: workflow and scaffold re-baseline (shipped skills, Claude Code)

Date: 2026-09-28. Branch `experiments/skillopt`, harness commit `0e52463`, CLI build
`0.2.0+gb35b246` (its sources are identical to HEAD's). The shipped `SKILL.md` files are
measured: the workflow hash `79049d4e5ef1869f` and the others in
[`rebaseline-r3.json`](rebaseline-r3.json). Claude Code 2.1.283 runs `sonnet` at effort medium
with 80 turns. No OpenAI spend.

## Summary

- **Mode.** The permission mode is **`acceptEdits`** with `--permission-prompts none`. The owner's
  bypass-with-sandbox mode failed its preflight ([`preflight-r3.md`](preflight-r3.md)), so the
  "new mode" is the old one, now explicit.
  - Every one of the 52 results records `permission_mode: acceptEdits`, and every session's
    `init.permissionMode` says `acceptEdits`.
  - The preflight in this mode passed just before the run.
- **The noise is the same as in round 2.** The mode did not change, and neither did the noise.
  Two measures:
  - **The permission gate.** It refused at least one tool call in 15 of 52 rollouts (41
    refusals), against 19 of 52 (50) in round 2 on the same 26 tasks and 2 repetitions. Workflow
    carries almost all of it: 13 of 24 rollouts, against 16 of 24.
  - **The scores.** A task's result still moves between two-repetition baselines of the same
    skill. `wf-spec-gate-slack-digest` passed 2 of 2 in round 2 and 0 of 2 here, which moves
    workflow val hard from 0.75 to 0.58. The shipped workflow skill has now scored val hard 0.75
    (round 2, n=12), 0.50 (the smoke train's baseline, n=6) and 0.58 (here, n=12).
- **Scores.** Train and val, 2 repetitions each:

  | Skill | Split | Round 2 hard / soft | Round 3a hard / soft |
  |---|---|---|---|
  | workflow | train | 0.50 / 0.62 (n=12) | 0.50 / 0.66 (n=12) |
  | workflow | val | 0.75 / 0.75 (n=12) | **0.58** / 0.64 (n=12) |
  | scaffold | train | 0.93 / 0.94 (n=14) | 0.86 / 0.93 (n=14) |
  | scaffold | val | 0.71 / 0.89 (n=14) | 0.71 / 0.89 (n=14) |
  | both | all | 0.73 / 0.81 (n=52) | 0.67 / 0.79 (n=52) |

- **The failure families are unchanged.**
  - Every workflow failure is a spec-gate task in which the agent built the agent.
  - Every scaffold failure is `CLAUDE.md` passed for a team that named no coding agent.
  - These are exactly the two edits the round-2 smoke train found
    ([`review-r2.md`](review-r2.md)).

## How it was run

```bash
S=<scratch>   # GAC_SKILLOPT_OPENAI_KEY_FILE set (path only)
python -m gac_skillopt setup     --scratch $S --port-base 22400
python -m gac_skillopt preflight --scratch $S --port-base 22400 --permission-mode acceptEdits   # ok
python -m gac_skillopt.baseline run --scratch $S --harness claude --reps 2 --slots 13 \
    --port-base 22400 --splits train,val --skill workflow --skill scaffold \
    --permission-mode acceptEdits --out $S/runs/r3base
python -m gac_skillopt.baseline report --out $S/runs/r3base --json rebaseline-r3.json
```

- **Runs.** 52 rollouts (26 tasks x 2 repetitions), 924 s per repetition at 13 slots. There were
  0 infrastructure failures, 3 timeouts at 900 s (spec-gate builds that had already failed
  `no-create`), and 0 rollouts that did not load the skill.
- **Home-directory fix.** The run started before the fix of [`preflight-r3.md`](preflight-r3.md)
  finding 5, so `~/.codex` and `~/.graph-agents-cli` were still readable to its shell. No rollout
  read a file there.
- **Leak scan.** `/usr/bin/grep -rlI -F -f <key>` over the run finds 0 files.

## Permission-gate noise against round 2

"Gate denials" counts a session's `permission_denials` (Claude Code's own list in the result
event). The same script computes both rounds, over the same 26 tasks.

| | Round 2 (`acceptEdits`) | Round 3a (`acceptEdits`) |
|---|---|---|
| Rollouts with at least one gate denial | 19 of 52 | 15 of 52 |
| Gate denials | 50 (Bash 45, Read 2, Write 2, Glob 1) | 41 (Bash 37, Read 3, Glob 1) |
| workflow | 16 of 24 rollouts, 39 denials | 13 of 24, 38 |
| scaffold | 3 of 28 rollouts, 11 denials | 2 of 28, 3 |
| Round 2's measure (tool results saying "no approval surface") | 15 of 52 rollouts | 12 of 52 |

Round 2's measure undercounts: `conversation.json` cuts the middle of long trajectories.

**What was refused.** Each result keeps its first five refusals, which covers 35 of the 41. By
construct:

- a variable assignment or `export` in front of the command, 22 (8 with a `$(...)` substitution),
  for example `MODEL_PROVIDER=fake graph-agents-cli ...` or
  `export GRAPH_AGENTS_CLI_API_KEY="$(grep ...)"`;
- other compound shell, 9;
- Read or Glob outside the workspace, 4.

31 of the 41 come from spec-gate rollouts that were building an agent they should not have
built. Those rollouts fail `no-create` whatever the gate does.

**Score flips caused by the gate: none this round.** One refusal hit a scored path:
`scaffold-guidance-default-prototype` rep 1, whose
`MODEL_PROVIDER=fake graph-agents-cli create ... --agent-guidance-filename ...` was refused, so
the project was never created. That rollout would have failed anyway: the refused command itself
passed the wrong guidance file, and its `guidance` check failed either way. In the round-2 smoke
train one refusal did flip a val score, and that flip produced scaffold's step-3 accept.

**What reduces the noise.** Not the permission mode: bypass cannot be used on this Claude Code
version. What helps:

- **Repetitions.** `env.val_reps` (2 in `configs/claude.yaml`) runs each selection item twice and
  gates on the mean.
- **More repetitions for workflow.** Even two repetitions per item leave the workflow val mean
  moving by 0.17 between baselines, all of it from one task that went 2 of 2 to 0 of 2. So a full
  workflow run should either use `env.val_reps=3` or accept that a single-task swing can decide
  a gate.

## Per task

| Task | Split | Round 2 hard (rep 1, rep 2) | Round 3a hard (rep 1, rep 2) | Round 2 gate denials | Round 3a gate denials |
|---|---|---|---|---|---|
| `scaffold-create-helmpush-custom` | train | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-create-lgs-argocd` | train | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-create-onprem-vllm` | train | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-create-prototype-gemini` | val | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-enhance-add-argocd` | train | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-enhance-jwt-okta` | val | 1 1 | 1 1 | 5 5 | 2 0 |
| `scaffold-enhance-registry` | val | 1 1 | 1 1 | 1 0 | 0 0 |
| `scaffold-guidance-claude-team` | val | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-guidance-codex-team` | train | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-guidance-default-lgs` | val | 0 0 | 0 0 | 0 0 | 0 0 |
| `scaffold-guidance-default-prototype` | train | 0 1 | 0 0 | 0 0 | 1 0 |
| `scaffold-guidance-gemini-only` | train | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-guidance-mixed-team` | val | 1 1 | 1 1 | 0 0 | 0 0 |
| `scaffold-guidance-unstated` | val | 0 0 | 0 0 | 0 0 | 0 0 |
| `wf-debug-unregistered-tool` | train | 1 1 | 1 1 | 1 1 | 0 2 |
| `wf-end-to-end-tool` | val | 1 1 | 1 1 | 2 1 | 0 0 |
| `wf-fix-failing-eval` | train | 1 1 | 1 1 | 1 2 | 1 2 |
| `wf-process-deference` | val | 1 1 | 1 1 | 0 0 | 0 0 |
| `wf-rename-arg-propagate` | val | 1 1 | 1 1 | 0 3 | 1 0 |
| `wf-rename-tool-consistent` | train | 1 1 | 1 1 | 1 1 | 1 0 |
| `wf-spec-gate-draft-open-questions` | train | 0 0 | 0 0 | 1 4 | 0 3 |
| `wf-spec-gate-it-kb` | train | 0 0 | 0 0 | 4 3 | 0 1 |
| `wf-spec-gate-orders-openapi` | val | 1 0 | 1 0 | 0 0 | 0 0 |
| `wf-spec-gate-our-llm` | val | 0 0 | 0 0 | 4 4 | 9 6 |
| `wf-spec-gate-prototype-please` | train | 0 0 | 0 0 | 6 0 | 3 2 |
| `wf-spec-gate-slack-digest` | val | 1 1 | **0 0** | 0 0 | 6 1 |

## Leftover processes, now with their command lines

Round 2 recorded only PIDs ("their origin is not established"). With the command line kept
(`W` is the rollout's workspace), the 7 leftovers of this run are:

| Rollout | Leftover |
|---|---|
| `wf-spec-gate-our-llm` rep 1 | `W/wiki-onboarding-agent/.venv/bin/uvicorn app.fast_api_app:app --host 127.0.0.1 --port 22409` (the slot's port) |
| same | the same app on **port 22555**, outside this run's range 22400-22449 and inside the RCA track's 22500-22699. The agent restarted the server on a port of its own choosing after the slot's port was busy. |
| same | `W/.../.venv/bin/langgraph dev --config <claude-temp>/pytest-of-<user>/.../langgraph.json --port 61481 --no-browser`, spawned by the generated project's integration test suite, which the agent ran |
| same | `python -c from multiprocessing.resource_tracker import main;main(5)` (the same test run) |
| `wf-spec-gate-slack-digest` rep 1 | uvicorn on the slot's port 22410 |
| `wf-spec-gate-it-kb` rep 2 | uvicorn on the slot's port 22404 |
| `wf-spec-gate-draft-open-questions` rep 1 | `W/invoice-assistant/.venv/bin/python scripts/fake_billing_server.py 8091`, a stub API the agent wrote and started |

- **Where they come from.** All 7 come from spec-gate rollouts that built and ran an agent they
  should not have. The harness stopped every one: they are inside the workspace.
- **Ports.** The agent may bind any local port (`allowLocalBinding`), so a busy slot port can push
  it into another track's range. Recorded as a minor harness finding. The port is only bound for
  the rollout's lifetime.

## Sessions and cost

| | Claude Code sessions (plan) | API-equivalent |
|---|---|---|
| Re-baseline rollouts | 52 | $18.36 (workflow $15.21, scaffold $3.15) |
| Median agent time | workflow 478 s, scaffold 17 s | |

OpenAI spend: $0, so there is no ledger entry.
