# Codex transfer check of the Claude-derived workflow and scaffold texts (round 3b)

Date: 2026-09-28. Branch `experiments/skillopt`, CLI build `0.2.0+g87da010`. Codex 0.154.0,
`gpt-5.6-terra`, effort medium, the isolation of DESIGN section 3.2. This check is part of the
same step as the observability run ([`train-r3-observability.md`](train-r3-observability.md)),
and every rollout is in [`train-r3-observability.json`](train-r3-observability.json) under
`runs.transfer-*`.

**The question:** the workflow and scaffold texts the owner approved in round 3a were optimised
and confirmed on Claude Code. Do those edits hurt or help Codex?

- The texts are the bodies of [`review-r2-workflow.md`](review-r2-workflow.md) and
  [`review-r2-scaffold.md`](review-r2-scaffold.md).
- Before any run, the body sha256s were checked: workflow `efe6ca364df8d20e…`, scaffold
  `2b78aeb982d77e43…`. Both match [`review-r2.md`](review-r2.md) and the owner's decision.

## Answer

- **Scaffold: neutral on Codex.** The approved text and the shipped one both scored 18 of 18:
  - test, 2 tasks × 2 repetitions: 4 of 4 each;
  - val, 7 tasks × 2 repetitions: 14 of 14 each.

  Codex already kept `AGENTS.md` when no single agent was named. The Claude-derived guidance-file
  rule neither helps nor hurts it.
- **Workflow on the test split (the ask): no difference, and both fail.**
  - The one frozen test task, `wf-spec-gate-new-agent`, scored 0 of 2 under each text.
  - In all four rollouts Codex stopped before `create` and wrote a draft spec, as the gate wants.
  - It then asked no question (`asks`) and did not raise the data source (`covers-data`).
  - The approved text's rollouts ended "Please approve the spec to proceed". The shipped text's
    ended "I stopped before scaffolding because the installed workflow requires spec approval".
- **Workflow on val (beyond the ask): the approved text hurts Codex where it must proceed.**
  - **The regression.** On the three val tasks that must proceed with a concrete change to an
    existing project, the approved text passed **6 of 12** and the shipped text **12 of 12**
    (4 repetitions each, Fisher p = 0.014).
    - `wf-end-to-end-tool`: 0 of 4 against 4 of 4.
    - `wf-rename-arg-propagate`: 2 of 4 against 4 of 4.
    - `wf-process-deference`: 4 of 4 each.
  - **How the failing rollouts failed.** Each one wrote a draft spec and refused the change.
  - **The spec-gate val tasks: no hard difference.** Both texts scored 0 of 6, every failure on
    `asks`. The approved text stopped before `create` in 6 of 6, and the shipped text in 5 of 6
    (`wf-spec-gate-slack-digest` rep 2 built the agent).
- **Net: on Codex, the approved workflow text trades a small gain in stopping for a large loss on
  ordinary change requests.** On Claude the same text did not over-stop: 7 of 9 on those tasks in
  round 3b's head-to-head, and both failures were check false negatives
  ([`train-r3-workflow.md`](train-r3-workflow.md)). **The regression is Codex-specific.**
- **The round-3b proposed workflow text, also beyond the ask.** It is the approved text plus
  training's step-1 edit (open decisions as questions ending in `?`), and it is not yet approved
  ([`review-r3-workflow.md`](review-r3-workflow.md), body sha256 `508d1ceb…`). On Codex, 2
  repetitions:
  - **Its question edit transfers.** The spec-gate val tasks pass 6 of 6 hard, against 0 of 6
    for the approved text (p = 0.002). Every failure there had been `asks`.
  - **Test:** 1 of 2. The failure was `covers-data`.
  - **It keeps the over-stop.** `wf-end-to-end-tool` 0 of 2, `wf-rename-arg-propagate` 2 of 2,
    `wf-process-deference` 2 of 2.

## Why the approved text over-stops on Codex

The approved text adds "What counts as approval" to Phase 0 (body lines 96-107):

```text
**What counts as approval.** Only the user explicitly approving the spec, in the conversation.
None of these is approval:

- a request to build the agent, however direct;
...
When the spec is not approved and nobody can approve it, the safe action is to stop before
`create`, `scaffold enhance`, or any agent code. Do these instead:
```

Codex applies it to a request to change an existing project that has no spec file:

- `wf-end-to-end-tool`, rep 1: "The installed project workflow forbids editing agent code or
  running evals until that spec receives explicit approval, even for this small local-tool change.
  Therefore I did not add the tool or eval case".
- `wf-rename-arg-propagate`, rep 2: "I could not make the runtime rename: the project's installed
  workflow requires an approved spec before agent-code changes, and none existed."

Round 2's review predicted this for the optimizer's first wording (`review-r2.md`, W1 risk 3,
"Unscoped stop ... could stop legitimate unattended code work on an existing project"). The final
wording tied the stop to "the spec is not approved", which is enough for Claude. Codex reads a
missing spec as an unapproved one.

**Where this matters now.** The owner approved this text for write-back on `v0.3` (round-3b
decision 2), and round 3b's V0 step writes it into both skill copies. The Codex regression is a
reason to scope the rule before the release. **Owner decision (not done here, not measured):**

- add a sentence such as "A concrete change to an existing project (add this tool, rename that
  argument, fix this failing eval) is not a new agent: make the change; the spec gate decides what
  a new agent is";
- measure it on Codex (the three tasks that must proceed and the three spec-gate tasks, 2
  repetitions, about $3) and on Claude (about 18 sessions);
- or run Codex SkillOpt on the workflow skill from the approved text. Its train split has three
  tasks that must proceed (`wf-rename-tool-consistent`, `wf-fix-failing-eval`,
  `wf-debug-unregistered-tool`).

## Method

| Run | Command (all `python -m gac_skillopt.baseline run --harness codex --model gpt-5.6-terra`) | Time | Codex cost (with the cache-write correction) |
|---|---|---|---|
| Test, the ask | `--reps 2 --splits test --skill workflow --skill scaffold --slots 3`, approved (`--body-dir`, port base 22160) and shipped (22166) started 3 s apart | 07:23-07:25 | $1.37 |
| Val, beyond the ask | `--reps 2 --splits val --skill workflow --skill scaffold --slots 5`, the same pair of port bases | 07:29-07:40 | $8.89 |
| Val, repetitions 3-4 of the tasks that must proceed | `--rep-start 3 --reps 2 --task wf-end-to-end-tool --task wf-rename-arg-propagate --task wf-process-deference` | 07:55-08:05 | $2.42 |
| The round-3b proposed text | `--reps 2 --splits val,test --skill workflow --body-dir <r3 proposed>` | 08:05-08:15 | $3.02 |

- **Infrastructure.** No infrastructure errors and no timeouts. Every rollout loaded the skill
  under test.
- **Reading the val numbers.** Val was the selection set of the Claude training that produced the
  approved text, so val is if anything tilted towards it. It still loses on Codex.
- **Significance.** At 2 repetitions, only the two pooled comparisons above are significant.

## Per task

Hard passes over the repetitions. The failed mandatory checks are named once per failing
rollout.

| Task | Split | shipped | approved | round-3b proposed | Failures |
|---|---|---|---|---|---|
| `wf-spec-gate-new-agent` | test | 0/2 | 0/2 | 1/2 | shipped and approved: `asks`, `covers-data` × 2 each; proposed: `covers-data` × 1 |
| `wf-end-to-end-tool` | val | 4/4 | **0/4** | **0/2** | approved and proposed: `tool`, `case`, `gate`, `proved` (a spec was written; nothing built) |
| `wf-rename-arg-propagate` | val | 4/4 | **2/4** | 2/2 | approved: `renamed`, `dataset`, `proved` × 2 |
| `wf-process-deference` | val | 4/4 | 4/4 | 2/2 | - |
| `wf-spec-gate-orders-openapi` | val | 0/2 | 0/2 | 2/2 | shipped and approved: `asks` |
| `wf-spec-gate-our-llm` | val | 0/2 | 0/2 | 2/2 | shipped and approved: `asks` |
| `wf-spec-gate-slack-digest` | val | 0/2 | 0/2 | 2/2 | shipped: `asks`, and rep 2 built the agent (`no-scaffold`, `no-create`); approved: `asks` |
| `scaffold-refuse-memory-k8s` | test | 2/2 | 2/2 | - | - |
| `scaffold-create-process` | test | 2/2 | 2/2 | - | - |
| 7 scaffold val tasks | val | 14/14 | 14/14 | - | - |

**Soft on the spec-gate val tasks.** The approved text scored 0.80-0.83 in every rollout. The
shipped text scored 0.40-0.83, and its one agent that built scored 0.40.
