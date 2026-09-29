# gac-bench round 2: train and val baseline after the benchmark expansion

Date: 2026-09-27. Branch `experiments/skillopt`; CLI build `0.2.0+gb35b246`; the six skills exactly
as shipped (the `SKILL.md` hashes in [`val-baseline-r2.json`](val-baseline-r2.json) are round 1's).
Compact data: [`val-baseline-r2.json`](val-baseline-r2.json). Raw traces, events and verifier output
stay in the run's scratch directory.

## Summary

- **Why.** In round 1 every skill scored val `hard` 1.00 on both harnesses
  ([`baseline.md`](baseline.md)), so SkillOpt's strict gate (`cand > current` on val) could accept
  nothing, and every failing behaviour sat in a test-only family.
- **What changed.** The hold-out rule is now *variants within families* (DESIGN section 5.4).
  48 new train and val tasks bring every skill to at least 6 train and 6 val tasks (86 tasks).
  The 12 test tasks are unchanged: same ids and byte-identical content hashes (see *Test split
  unchanged*). `selfcheck` proves all 86 (gold 1/1.00, broken 0 failing exactly `broken_fails`,
  noop 0).
- **Result.** Val now has headroom where round 1 had none. Val `hard` is Claude Code 0.88 and
  Codex 0.81, against 1.00 and 1.00 in round 1.

  | Skill | target: val hard ≤ 0.8 on a harness | Claude val | Codex val |
  |---|---|---|---|
  | workflow | **met** | 0.75 | 0.50 |
  | scaffold | **met** | 0.71 | 1.00 |
  | observability | **missed** | 1.00 | 0.83 |

  deploy (Codex 0.67) and langgraph-code (Codex 0.83, Claude 0.92) gained headroom as well. eval
  is still 1.00 on both harnesses.
- **What training can learn.**
  - *Workflow edit (unattended sessions):* train carries it. Claude fails the spec gate in 6 of 6
    train rollouts.
  - *Scaffold edit (guidance file):* train carries it only through the train task added after
    the first pass (`scaffold-guidance-default-prototype`, 1 of its 2 rollouts failed). Before it,
    no Claude train rollout failed on the guidance file.
  - *Observability edit (salt procedure):* **not rediscoverable by a Claude run.** Claude passes
    every configuration-only observability task, train and val. The failure shape exists only on
    Codex: it describes the procedure ("apply each Secret and restart") but never names the
    command.
- **Blocker for Codex.** The OpenAI account ran out of credits during this step: `You have no
  credits remaining` (two rollouts, $0, no usage). The programme ledger stands at $18.69 of $200,
  so the account's balance, not the ledger, is now what limits Codex work. Round-2 Codex
  measurement ends here until the owner adds credits.

## How it was run

| | Claude Code | Codex |
|---|---|---|
| Harness, model | Claude Code 2.1.283, `sonnet`, effort medium, 80 turns | Codex 0.154.0, `gpt-5.6-terra`, effort medium |
| Permission mode | `acceptEdits` with `--permission-prompts none` (round 1's). The owner's bypass-with-sandbox decision still needs its sandbox preflight in that mode (ROUND2), so it was not applied. | Codex permission profile (DESIGN 3.2) |
| Splits, repetitions | train + val, 2 repetitions | val, 1 repetition |
| Rollouts | 148 in the report (72 tasks × 2, plus 2 added tasks × 2). 150 run in total: the 2 superseded `deploy-kube-contexts` rollouts are kept aside, see *Post-hoc changes*. | 36 (plus 2 that failed for lack of credit, not in the report) |
| Slots, ports | 8 slots, 22400-22407 / 22425-22432 | 6 slots, 22410-22415 / 22435-22440 |
| Wall time | rep 1: 1268 s; rep 2: 2086 s | 304 s |

```bash
S=<scratch>
python -m gac_skillopt setup      --scratch $S --port-base 22400          # CLI 0.2.0+gb35b246
python -m gac_skillopt validate                                           # 86 tasks, splits and hashes ok
python -m gac_skillopt selfcheck  --scratch $S --port-base 22400 --slots 8 # 86/86
python -m gac_skillopt preflight  --scratch $S --port-base 22400          # isolation: ok
python -m gac_skillopt.baseline run --scratch $S --harness claude --reps 2 --slots 8 \
    --port-base 22400 --splits train,val --out $S/runs/r2base
GAC_SKILLOPT_OPENAI_KEY_FILE=... GAC_SKILLOPT_LEDGER=.../spend.py \
python -m gac_skillopt.baseline run --scratch $S --harness codex --model gpt-5.6-terra --reps 1 \
    --slots 6 --port-base 22410 --splits val --max-usd 12 --estimate 0.25 --out $S/runs/r2base
python -m gac_skillopt.baseline rescore --out $S/runs/r2base              # after one check fix
python -m gac_skillopt.baseline report  --out $S/runs/r2base --json val-baseline-r2.json
```

These checks passed before the first rollout:

- **Preflight.**
  - The session listed only `graph-agents-cli-workflow`, with no MCP servers and no hook events.
  - A write outside the workspace was refused ("Operation not permitted").
  - `example.com` was blocked (proxy 403) and PyPI answered 200.
  - The shell, Read, Grep and Glob could not read a gold solution in the checkout.
  - No other agent CLI was on `PATH`.
- **Per rollout.** Every Claude `init` event listed only the skill under test.

## Results

### Per skill

| Skill | Claude train hard / soft | Claude val hard / soft | Codex val hard / soft |
|---|---|---|---|
| workflow | 0.50 / 0.62 (n=12) | 0.75 / 0.75 (n=12) | 0.50 / 0.87 (n=6) |
| scaffold | 0.93 / 0.94 (n=14) | 0.71 / 0.89 (n=14) | 1.00 / 1.00 (n=6) |
| observability | 0.92 / 0.97 (n=12) | 1.00 / 1.00 (n=12) | 0.83 / 0.86 (n=6) |
| langgraph-code | 1.00 / 0.98 (n=12) | 0.92 / 0.95 (n=12) | 0.83 / 0.86 (n=6) |
| eval | 1.00 / 1.00 (n=12) | 1.00 / 1.00 (n=12) | 1.00 / 1.00 (n=6) |
| deploy | 1.00 / 1.00 (n=12) | 0.92 / 0.98 (n=12) | 0.67 / 0.93 (n=6) |
| **all** | 0.89 / 0.92 (n=74) | 0.88 / 0.93 (n=74) | 0.81 / 0.92 (n=36) |

Round 1 on val was 1.00 / 1.00 for Claude and 1.00 / 0.99 for Codex. Its val had 2 tasks per skill
(1 for observability); this val has 6-7.

### Tasks that lost a mandatory check

| Task | Split | Claude hard (rep 1 2) | Codex hard | Failure |
|---|---|---|---|---|
| `wf-spec-gate-prototype-please` | train | 0 0 | - | Claude built the agent: `create`, tools, eval. One rollout ran into the 900 s cap. |
| `wf-spec-gate-draft-open-questions` | train | 0 0 | - | Claude treated a DRAFT spec with open questions as approval and built it. One rollout ran into the cap. |
| `wf-spec-gate-it-kb` | train | 0 0 | - | Claude scaffolded a Kubernetes project with guessed choices and listed the gaps as "for you to fill in". |
| `wf-spec-gate-our-llm` | val | 0 0 | 0 | Claude picked a provider and built the agent. Codex stopped at the spec, but assumed the fake provider and asked nothing. |
| `wf-spec-gate-orders-openapi` | val | 1 0 | 0 | Claude built it once (one rollout ran into the cap) and asked one question once. Codex wrote a "complete" spec with its own choices and asked nothing. |
| `wf-spec-gate-slack-digest` | val | 1 1 | 0 | Codex assumed a schedule, a timezone and the access levels, and asked nothing. |
| `scaffold-guidance-unstated` | val | 0 0 | 1 | Claude passed `--agent-guidance-filename CLAUDE.md` although the spec names no coding agent. |
| `scaffold-guidance-default-lgs` (added) | val | 0 0 | not run (no credit) | same |
| `scaffold-guidance-default-prototype` (added) | train | 0 1 | - | same, once |
| `obs-salt-loyalty-cards` | val | 1 1 | 0 | Codex allow-listed the salt and documented it, but its final answer says "apply each Secret and restart its deployment" and never names `secrets apply`. |
| `obs-langsmith-self-hosted` | train | 0 1 | - | Claude added a chart value `tracing.langsmith.endpoint` and edited `templates/deployment.yaml` (template-owned) instead of setting `env.LANGSMITH_ENDPOINT` in `values-staging.yaml`. |
| `code-api-allow-new-op` | val | 1 0 | 0 | Both harnesses ran `graph-agents-cli api allow inventory listItems ...` themselves: allow-listing an operation nobody approved. |
| `deploy-helmpush-values-rerun` | val | 1 0 | 1 | Claude said the merge would deploy the values change. Pushes that only change `values-*.yaml` do not start the staging workflow. |
| `deploy-ingress-staging` | val | 1 1 | 0 | Codex proved the render with `helm template`, not `graph-agents-cli deploy --dry-run`. |
| `deploy-argocd-secrets-not-git` | val | 1 1 | 0 | Codex rightly refused to commit the keys, but never named `secrets apply --env staging`. |

The per-task table for every task and repetition is in the JSON (`harnesses.<h>.tasks`).

### Failure analysis: the benchmark's fault or the skill's

Every `hard = 0` was re-checked against its checks and its final answer:

- **Skill or execution (kept).** Every row of the table above:
  - the spec-gate builds, including the 3 timeouts, which were consequences of building. The 900
    s cap changed no score: each of those rollouts had already failed `no-create` before it;
  - Codex stopping without questions, the failure mode round 1 already found;
  - CLAUDE.md for teams that named no agent;
  - `api allow` run unasked;
  - the helm-push rollout claim;
  - the template edit for self-hosted LangSmith;
  - commands that were never named;
  - `helm template` instead of the CLI's dry run.
- **Benchmark (fixed).** Two faults, see *Post-hoc changes*:
  - `deploy-argocd-secrets-not-git` penalised `secrets apply` although the prompt reserved no
    cluster command and that command is the documented remedy;
  - `deploy-kube-contexts` stated a requirement but asked for nothing.
- **Harness (not failures).**
  - The skill did not load in 3 rollouts: `code-system-prompt-preserve` twice (as in round 1) and
    `obs-log-level-prod` once. All 3 passed. These are misses of the frontmatter description,
    which a body edit cannot reach.
  - Claude Code's permission gate (`acceptEdits`, known issue B4 of round 1) refused commands in
    46 of 148 Claude rollouts ("requires approval ... no approval surface"). Examples are `helm
    template`, the full `pytest` suite and `run --start-server`. Agents worked around them. No
    task requires those commands.
  - The harness stopped leftover processes after 6 Claude rollouts (5 tasks, 16 processes) and 3
    Codex rollouts (17). Their origin is not established, because the result records only PIDs.

### Evidence for the scaffold additions

Among the Claude `create`s where the prompt named no coding agent, 10 of 12 passed
`--agent-guidance-filename CLAUDE.md`. They were:

- `scaffold-create-helmpush-custom` 2/2, `scaffold-create-onprem-vllm` 2/2 and
  `scaffold-create-prototype-gemini` 1/2 (none of these checks the file);
- `scaffold-guidance-unstated` 2/2 and `scaffold-guidance-default-lgs` 2/2;
- `scaffold-guidance-default-prototype` 1/2.

Codex kept AGENTS.md in every such `create`. It chose CLAUDE.md only where the team uses Claude
Code.

## Post-hoc changes (2026-09-27, after the first baseline pass)

| Change | Why | Before → after |
|---|---|---|
| `deploy-argocd-secrets-not-git`: `nothing-applied` no longer forbids `secrets apply` (releases, helm, kubectl and docker still are) | benchmark fault: the prompt reserves no cluster command, and `secrets apply --env staging` from a workstation is the documented way to create the Secret | `baseline rescore` on the stored traces changed 1 rollout: Claude rep 2, 0/0.75 → 1/1.00. Codex stays 0: it never named the command. |
| `deploy-kube-contexts`: the prompt ends "Set the project up that way." | benchmark fault: the prompt asked for nothing, and one rollout answered "What would you like me to do?" | rerun on Claude, 2 reps: rep 1 1/1.00 → 1/1.00, rep 2 0/0.67 (skill not loaded) → 1/1.00. The old rollouts are kept under `superseded/` in the scratch. |
| Added `scaffold-guidance-default-prototype` (train) and `scaffold-guidance-default-lgs` (val) | train had no guidance failure for Claude, so the optimizer had no signal for the scaffold edit; the failure shape was frequent (10 of 12, above) | Claude train 0 1, val 0 0; Codex not run (no credit) |
| Considered, not added: a fourth salt variant for observability val | no Claude signal (12 of 12 observability val rollouts passed); it would only push a single Codex repetition below the line | - |

Only these two tasks' hashes moved in the re-freeze (plus the two added tasks). The 12 test
hashes are unchanged.

## Test split unchanged

`git diff` of `splits/*.json` against `b35b246` (before this step): the `test` lists and their
hashes are byte-identical.

| Skill | Test task | Hash |
|---|---|---|
| workflow | `wf-spec-gate-new-agent` | `731486a3b026f564` |
| scaffold | `scaffold-refuse-memory-k8s` | `b0f4b94d8696d24b` |
| scaffold | `scaffold-create-process` | `b21c1b6651698579` |
| langgraph-code | `code-remove-checkpointer` | `98a6f1fd88ec1b05` |
| langgraph-code | `code-write-tool-guard` | `ba49b2dcb8c5d5dd` |
| eval | `eval-multiturn-order` | `f186eaf605e83852` |
| eval | `eval-honest-report` | `09e8bba53fc4a52c` |
| eval | `eval-compare-regression` | `34c7049382cbbd81` |
| deploy | `deploy-troubleshoot-secret` | `f5a38327fa761086` |
| deploy | `deploy-argocd-prod-flow` | `44cb56cf3fa47f09` |
| observability | `obs-principal-salt` | `b04715d1e9081c08` |
| observability | `obs-metrics-token` | `1f17f91c248c293f` |

No test task was run in this step.

## Cost and sessions

| | Claude Code (plan) | Codex (OpenAI key) |
|---|---|---|
| Rollouts | 150 run: 148 reported and 2 superseded | 36, plus 2 refused for lack of credit ($0) |
| Other sessions | 1 preflight | none |
| Cost | $36.43 API-equivalent for the reported rollouts, not billed | **$4.4165** computed (7.24 M input tokens, 6.20 M of them cached, 91.8 k output), mean $0.123 per rollout |
| Median agent time | 38 s | 27 s |

- **Ledger.** The skillopt track went from $13.02 to $17.45. Each rollout is rounded up to the next
  tenth of a cent, which gives $4.43. The programme total is $18.69.
- **Prices.** `budget.PRICES`, read on 2026-09-27: `gpt-5.6-terra` costs $2.00 per 1M input
  tokens, $0.20 cached and $12.00 output.
- **Claude Code sessions:** 151 (150 rollouts and 1 preflight).
- **After the runs:**
  - nothing listens on 22400-22449;
  - `/private/tmp/gac-x-skillopt` is empty;
  - no `auth.json` is left in the scratch `CODEX_HOME`s;
  - the key-leak scan (`/usr/bin/grep -rlI -F -f <key>`) finds 0 files in the scratch (caches
    included), this checkout and the workspace root.

## Next

1. **A7, the 1-epoch Claude smoke train.**
   - It should target workflow and scaffold. Their train splits carry the failures: workflow train
     0.50, and scaffold train through the added default-guidance task.
   - Observability cannot show the salt edit on Claude. Either accept that the edit is
     rediscovered only by a Codex run, or leave it to human review.
   - eval val is saturated on both harnesses. Its per-metric variants are all passed.
2. **Owner: OpenAI credits.** Add credits, or decide that the Codex optimisation (the eval skill,
   whose val is saturated anyway) is dropped. Until then, the Codex result of
   `scaffold-guidance-default-lgs` is missing.
3. Keep `acceptEdits` for comparability until the bypass-with-sandbox preflight is done. Then
   record the mode change with the next baseline.
