# gac-bench round 3b: SkillOpt on the observability skill with Codex rollouts

Date: 2026-09-28. Branch `experiments/skillopt`, CLI build `0.2.0+g87da010` (the CLI sources are
unchanged since `b35b246`), SkillOpt at `79124b37`. Rollouts on Codex 0.154.0 `gpt-5.6-terra`,
billed to the OpenAI key. The optimizer is Claude Code `opus` through `bin/claude-isolated`, on the
owner's plan.

The run started from the **shipped** observability skill (body 10,478 characters, sha256
`930dde3b…`). Nothing was written to `skills/` or to the bundled copy. The human review of the
result, with the proposed final text, is [`review-r3-observability.md`](review-r3-observability.md).
The Codex transfer check of the approved workflow and scaffold texts is
[`transfer-r3-codex.md`](transfer-r3-codex.md). Every rollout of this round is in
[`train-r3-observability.json`](train-r3-observability.json).

## Summary

- **SkillOpt found the target.** The round-1 target was the salt procedure, marked as
  configuration-only (shipped `SKILL.md:81-91`).
  - **Proposed:** step 1's failure patch, from one train rollout (`obs-salt-phone-numbers`). Its
    final answer said "run your normal secret apply and rollout" and never named the commands.
  - **What the edit adds:** a new section, "Procedure: salt the hashed principal id". It says:
    - add `PRINCIPAL_HASH_SALT` to `secrets.keys`;
    - "This is configuration only ... do not change code, chart templates or `values-*.yaml`";
    - the user puts the value in `.env.<env>`;
    - then run `graph-agents-cli secrets apply --env <env>` and `graph-agents-cli deploy --restart
      --env <env>`;
    - a user who runs the cluster commands gets the commands verbatim.
  - **Accepted** by the gate in epoch 1.
- **Training ran 2 epochs, 4 steps, in 12.7 minutes: 1 accept and 3 rejects.** The slow update
  was also rejected.
  - The gate score (mixed, val × 3 repetitions) went from 0.859 to 0.956 at step 1.
  - Val hard went from 15/18 to 18/18.
  - The one failing val task, `obs-salt-loyalty-cards`, went from 0/3 to 3/3.
  - **Every later candidate was stopped by the fact-check's size cap before any val rollout.**
    Those were steps 2, 3 and 4 and the slow update. The step-1 body is already +20.7 %, and the
    cap is +25 % of a short skill.
- **Fresh head-to-head on Codex, 3 repetitions each, both bodies run at the same time:**

  | Body | val hard | val soft | test hard | test soft | `obs-salt-loyalty-cards` | `obs-principal-salt` names `secrets apply` |
  |---|---|---|---|---|---|---|
  | shipped | 0.83 ± 0.00 (15/18) | 0.85 ± 0.01 | 1.00 (6/6) | 0.90 ± 0.00 | 0 of 3 | 0 of 3 |
  | best (step 1) | 1.00 ± 0.00 (18/18) | 0.93 ± 0.06 | 1.00 (6/6) | 1.00 ± 0.00 | 3 of 3 | 3 of 3 |

  - `±` is the standard deviation of the three repetitions' means.
  - Val hard 15/18 against 18/18 gives Fisher p = 0.23.
  - **The target task is where the gain is.** Pooling every fresh measurement of
    `obs-salt-loyalty-cards` (none of them chose a winner):
    - shipped: 1 of 9;
    - best body and proposed text: 6 of 6;
    - Fisher p = 0.001.
  - **On the frozen test task `obs-principal-salt`,** both bodies pass the mandatory checks. The
    non-mandatory `next-step` check shows the target behaviour: the shipped skill never named
    `secrets apply` (0 of 3), and the best body always did (3 of 3).
- **The edit also narrows what Codex changes.** Across the 9 salt rollouts of each body in the
  head-to-head, the shipped skill's agents edited:
  - `README.md` in 5;
  - `.env.example` in 4;
  - `app/app_utils/auth.py` in 2;
  - unit tests in 2.

  The best body's agents edited only the manifest, `.env` where the task needs it, and in one
  rollout a draft spec file.
- **No regression on Claude Code.** Both bodies scored 24/24 hard on val and test (`acceptEdits`,
  3 repetitions). Soft was 0.961 for the best body against 0.975, and 1.00 on test for both.
  Observability is saturated on Claude, so this is only a regression check.
- **The proposed final text** was measured on Codex val, 3 repetitions: 18/18 hard, soft 0.922.
  It is the best body with four reviewed changes (see the review).
- **Harness.** Checking the harness first found a blocker, and the run found four more gaps. All
  five are fixed in separate commits (*Harness findings*).
  - **The blocker:** since `eadd7ee`, every Codex rollout failed before the model was called.
  - **One of the gaps:** Codex spend was under-recorded by about 12 %, because the harness priced
    cache writes as plain input. The ledger now carries the corrections.
- **Cost:**
  - **OpenAI:** $38.34 for this step, against its $45 budget. That is $33.85 as recorded by the
    harness, plus a $4.19 cache-write correction and a $0.30 estimate for one killed session.
    The skillopt track stands at $57.73 of $90.
  - **Codex rollouts:** 237, plus 3 live isolation probes.
  - **Claude Code:** 63 sessions on the plan.

## Run setup

| | |
|---|---|
| Command | `python -m gac_skillopt.run train --config configs/codex.yaml --skill observability --cfg-options train.num_epochs=2 train.batch_size=5 evaluation.eval_test=false env.val_reps=3 env.slots=9 env.port_base=22150 env.factcheck=true env.max_usd=25 env.harness_model=gpt-5.6-terra` |
| Config | `configs/codex.yaml` on `configs/claude.yaml`: 2 epochs, batch 5 (6 train tasks make steps of 5 and 1), the whole val split, the slow update with 5 pairs gated on selection, meta skill on |
| Rollouts | Codex 0.154.0, `gpt-5.6-terra`, effort medium, the scratch `CODEX_HOME` and `HOME` and the `rollout` permission profile (DESIGN section 3.2). The run cap is `env.max_usd=25`, and the ledger is checked before every rollout, now including the track's cap. |
| Optimizer | `opus` (claude-opus-5-5), effort high, through `bin/claude-isolated` (`config.json`: `claude_code_exec_path` is the wrapper, `claude_code_exec_use_sdk: cli`). 14 sessions, 0 failures. |
| Gate | `mixed` (0.5 hard + 0.5 soft), strict `cand > current`, the whole val split, **3 repetitions per task** |
| Fact-check | on: sections kept, no unknown command or flag, body at most 1.25 × the shipped 10,478 characters (13,097). `secrets apply --env` and `deploy --restart` are known to it (checked before the run). |
| Test split | off in training (`evaluation.eval_test=false`). Neither `obs-principal-salt` nor `obs-metrics-token` appears in any training prediction. They were run only in the head-to-head. |
| Ports | 22150-22199 for every run of this step |
| Isolation | Checked before any billed run (*Harness checks*). `GAC_SKILLOPT_DENY_READ` also named every other scratch area of this session, the other sessions' temp directories, `~/.claude/{projects,skills,plugins}`, `~/.agents` and the owner-specific paths |

## Harness checks before the run

1. **A stale CLI is refused.**
   - The round-1 scratch holds a CLI built at `9c93fd2`, before the round-2 `src/` fixes.
   - Pointed at it, `selfcheck` exits 1 with "the scratch CLI ... is stale: the CLI sources (src,
     pyproject.toml, hatch_build.py) changed since the build's commit 9c93fd2".
   - It wrote nothing: no new run directory, and the refusal comes before the wrapper is written.
   - The fresh scratch was built with `setup --rebuild-cli`. `_build_info.json` records commit
     `87da010`, dirty false, and `cli_staleness` is empty at every later HEAD of this step, because
     none of them changes `src/`.
2. **Codex isolation, no model call.**
   - The harness's own `codex_config_toml` and `codex_env` were used with
     `codex sandbox -P rollout`. All 23 probes behaved as intended:
     - denied: `~/.codex`, `~/.codex/auth.json`, `~/.graph-agents-cli`, the key directory and
       file, the checkout's gold solutions, the main checkout, `<scratch>/runs`, the scratch
       `CODEX_HOME`, `~/.ssh`, and the `DENY_READ` areas (the A8 scratch, `~/.claude/projects`,
       the session's task outputs, the ledger);
     - denied writes: `$HOME`, the scratch, `/tmp`;
     - allowed: writes inside the workspace;
     - network: example.com gets `CONNECT tunnel failed, response 403`, and PyPI returns 200.
3. **Codex isolation, one live session. This found the blocker.**
   - The first live `codex exec` failed before the model was called:

     ```text
     Failed to create session: failed to load AGENTS.md instructions for environment `local`:
     fs sandbox helper failed ... sandbox-exec: execvp() of '/Users/<user>/.local/bin/codex'
     failed: Operation not permitted
     ```

   - The standalone Codex binary lives in `~/.codex/packages`. `eadd7ee` denied all of `~/.codex`.
   - Codex re-executes itself inside the sandbox, so since `eadd7ee` every Codex rollout would
     have failed.
   - Fixed in `540f89d`, which adds a `read` rule for `~/.codex/packages` only.
   - After the fix, one live session ran a probe script through the sandboxed shell. The shell
     was denied `~/.codex`, `~/.graph-agents-cli`, the key file and the scratch `CODEX_HOME`, was
     denied a write to `$HOME`, and was blocked from example.com. Cost: $0.04.
4. **Claude preflight** (`acceptEdits`, this step's deny list): `preflight: ok`. The only failure
   was the known non-gating `other_run_workspace_unreadable`.
5. **Key probe:** one `gpt-5-mini` call, status 200 ($0.0001, in the ledger).

## Training curve

| Phase | Epoch | Rollouts | hard | soft | Gate (mixed) | Decision |
|---|---|---|---|---|---|---|
| Baseline val (shipped) | - | 6 × 3 | 0.833 | 0.885 | 0.859 | - |
| Step 1 train | 1 | 5 | 0.800 | 0.859 | - | 1 failure: `obs-salt-phone-numbers` `names-apply` |
| Step 1 candidate val | 1 | 6 × 3 | 1.000 | 0.911 | 0.956 | **accept** (0.859 → 0.956), best |
| Step 2 train | 1 | 1 | 1.000 | 1.000 | - | success-only reflection |
| Step 2 candidate | 1 | 0 | - | - | - | **reject**: fact-check, 13,417 characters (cap 13,097) |
| Slow update, epoch 1 | 1 | 0 | - | - | - | empty placeholder |
| Step 3 train | 2 | 5 | 1.000 | 0.950 | - | success-only reflection |
| Step 3 candidate | 2 | 0 | - | - | - | **reject**: fact-check, 13,311 characters |
| Step 4 train | 2 | 1 | 1.000 | 1.000 | - | success-only reflection |
| Step 4 candidate | 2 | 0 | - | - | - | **reject**: fact-check, 13,121 characters |
| Slow update, epoch 2 | 2 | 5 + 5 train | 1.000 | 0.975 | - | **reject**: fact-check, 16,293 characters |
| Meta skill, epoch 2 | 2 | 0 | - | - | - | optimizer memory written |

- **Times.** Training started at 07:28:36 and ended at 07:41:16. SkillOpt's own total is 613 s.
  Step 1 took 248 s: 75 s of rollouts, 90 s of reflection and ranking, and 83 s of val.
- **After step 1, train is saturated.** Steps 2-4 and the slow update saw no hard failure, so all
  their edits came from success-only reflection.

### Val per task (hard over the 3 repetitions)

| Task | Baseline | Step 1 (best) |
|---|---|---|
| `obs-langsmith-key-no-traces` | 3/3 | 3/3 |
| `obs-langsmith-key-rotated` | 3/3 | 3/3 |
| `obs-log-level-prod` | 3/3 | 3/3 |
| `obs-otlp-collector-moved` | 3/3 | 3/3 |
| `obs-salt-local-dev` | 3/3 | 3/3 |
| `obs-salt-loyalty-cards` | **0/3** (`names-apply` × 3) | **3/3** |

### The first run, stopped after its baseline

The first training invocation (07:21-07:26) was stopped during step 1. Its baseline had a verifier
false negative:

- **What the agent ran.** `obs-langsmith-key-rotated` rep 2 searched the project with `rg -n -i
  'langsmith|tracing|secrets apply|deploy.*restart|helm upgrade|...'`.
- **What the check did.** `command_segments` split that line at the `|` inside the quoted pattern,
  so `nothing-applied` saw a command "helm upgrade" and failed the rollout.
- **Why it would bias the run.** An agent told to name `secrets apply` and `deploy --restart` is
  likely to search for exactly those strings. The false negative would therefore penalise the
  target edit, and reflection would have learned from a failure that never happened.
- **The fix.** `9f8aaa1` splits only outside quotes. Rescored over every recorded trace of the
  programme (2,164 traces, 2,980 transcript checks), exactly one result changes: this one.
- **The restart.** Training was restarted with the fixed verifier. The stopped run's 23 rollouts
  are kept in the JSON under `training_aborted`. Its baseline was 15/18 (`obs-salt-loyalty-cards`
  1 of 3, plus the false negative), and none of its results were used for a decision.

## Candidates

### Accepted: step 1 (epoch 1), failure patch plus success patches

The merge produced 5 edits, and the ranking kept 3 (budget 3). All 3 were applied.

```diff
@@ Procedure: enable tracing for an environment, after step 4 @@
+When told not to deploy, or when the user runs cluster commands, change only the target
+environment's `values-<env>.yaml` or `.env.<env>`; `values.yaml` and other environments stay as
+they are. End your answer with the exact commands still to run, and do not run them yourself:
+- `graph-agents-cli secrets apply --env <env>`, whenever a value goes into `.env.<env>` (even one
+  the user fills in).
+- Then the deploy (`deploy --restart --env <env>` after a Secret-only change).
@@ "Locally: ..." @@
-Locally: set `TRACING_ENABLED=true` and either key or endpoint in `.env`; `playground` and `run`
-pick it up.
+Locally: edit only the project's `.env` (no chart values, no code). Set `TRACING_ENABLED=true` plus
+either `LANGSMITH_API_KEY` or `OTEL_EXPORTER_OTLP_ENDPOINT=<collector URL>`; for OTLP leave the
+LangSmith key unset so the OTLP path is selected. Keep `TRACE_CAPTURE=metadata` unless the user
+explicitly asked for `full`. Do not add the LangChain/LangSmith SDK's own tracing variables (the app
+reads only the variables in the table above), and leave the other `.env` entries (model provider,
+API key) as they are. `playground` and `run` pick the settings up. If the collector is not running
+yet, say so in your answer; the configuration is still correct.
+
+## Procedure: salt the hashed principal id
+
+Use this when principal ids are guessable (emails, phone numbers, account numbers).
+
+1. Add `PRINCIPAL_HASH_SALT` to `secrets.keys` in `graph-agents-cli-manifest.yaml` and keep the existing keys. This is configuration only. The app already uses HMAC for the hash whenever the variable is set, so do not change code, chart templates or `values-*.yaml`, and never put the value in a values file.
+2. The user puts a long random value (for example `python -c "import secrets; print(secrets.token_hex(32))"`) in `.env.<env>` and keeps it stable.
+3. `graph-agents-cli secrets apply --env <env>`, then `graph-agents-cli deploy --restart --env <env>`. Pods read a changed Secret only when they start. Repeat both steps for every deployed environment.
+4. Every hash changes once. Older traces, logs and run records keep the old unsalted hashes until retention removes them.
+
+When the user runs cluster commands themselves, the final answer must list these commands verbatim, with the real environment name substituted (for example `--env prod`). A phrase such as "apply your secrets and roll out" is not enough.
```

- **The salt procedure** is the failure edit, with support 1, from `obs-salt-phone-numbers`. The
  analyst's summary gives two failures:
  - "the final answer said only 'run your normal secret apply and rollout' instead of naming
    `graph-agents-cli secrets apply --env prod` and `graph-agents-cli deploy --restart --env
    prod`";
  - "the skill ... gives no step-by-step procedure, so the agent also edited the chart template
    and README".
- **The other two edits** are success edits:
  - the commands still to run, with support 2, from the three-rollout success minibatch;
  - "Locally", with support 1, from `obs-local-otlp`.
- **Cut by the budget:** a success edit to the "Hashed principal id" section. It would have
  replaced "Changing the salt changes every hash." with a pointer to the rotation steps. The review
  restores a shorter form of it.

### Rejected

| Candidate | What it proposed (abridged) | Why rejected |
|---|---|---|
| Step 2 | In "enable tracing", check that `LANGSMITH_API_KEY` is in `secrets.keys` (add it if missing), never put the key in a values file, and do not create `.env.<env>` when the user said they will add the key. Also: check a result without a cluster by rendering the chart with `helm template ... -f values-<env>.yaml`, passing throwaway `--set` values for blank required fields | fact-check: 13,417 characters, before any val rollout |
| Step 3 | Rewrites step 2 of "enable tracing": the `secrets.keys` check; "a self-hosted LangSmith, also set `env.LANGSMITH_ENDPOINT` in the same file (it is not a secret, and the `tracing` block has no endpoint field)"; "`LANGSMITH_API_KEY` must not be in that environment's Secret" for OTLP | fact-check: 13,311 characters |
| Step 4 | Salt rotation: "The key is usually already in `secrets.keys` ... leave the manifest, code and values untouched. Generate a new long random value yourself. Replace only the `PRINCIPAL_HASH_SALT` line in `.env.<env>` ... Do not print the value in your answer." | fact-check: 13,121 characters, 24 over the cap |
| Slow update, epoch 2 | A protected "strategic guidance" section of 3,590 characters | fact-check: 16,293 characters |

## Head-to-head: shipped against best body (Codex val and test, 3 repetitions)

- **Command:** `python -m gac_skillopt.baseline run --harness codex --model gpt-5.6-terra --reps 3
  --splits val,test --skill observability --body-dir <dir> --slots 8`. Both bodies were started
  3 s apart, at port bases 22150 (best) and 22162 (shipped). 07:42-07:50.
- **Test:** the frozen test split (`obs-principal-salt`, `obs-metrics-token`) was used only here.
- **Infrastructure:** no infrastructure errors, and no rollout timed out. Every rollout loaded the
  skill.

| Task | Split | shipped, hard per rep | best, hard per rep | shipped failures |
|---|---|---|---|---|
| `obs-langsmith-key-no-traces` | val | 1 1 1 | 1 1 1 | - |
| `obs-langsmith-key-rotated` | val | 1 1 1 | 1 1 1 | - |
| `obs-log-level-prod` | val | 1 1 1 | 1 1 1 | - |
| `obs-otlp-collector-moved` | val | 1 1 1 | 1 1 1 | - |
| `obs-salt-local-dev` | val | 1 1 1 | 1 1 1 | - |
| `obs-salt-loyalty-cards` | val | 0 0 0 | 1 1 1 | `names-apply` × 3; rep 3 also `code-untouched` (edited `app/app_utils/auth.py`) |
| `obs-principal-salt` | **test** | 1 1 1 | 1 1 1 | (non-mandatory `next-step` × 3) |
| `obs-metrics-token` | **test** | 1 1 1 | 1 1 1 | - |

**Per repetition:**

| Body | Split | hard per rep | mean ± sd | soft per rep | mean ± sd |
|---|---|---|---|---|---|
| shipped | val | 0.833, 0.833, 0.833 | 0.833 ± 0.000 | 0.842, 0.863, 0.842 | 0.849 ± 0.012 |
| best | val | 1, 1, 1 | 1.000 ± 0.000 | 0.883, 0.917, 1.000 | 0.933 ± 0.060 |
| shipped | test | 1, 1, 1 | 1.000 ± 0.000 | 0.90, 0.90, 0.90 | 0.900 ± 0.000 |
| best | test | 1, 1, 1 | 1.000 ± 0.000 | 1.00, 1.00, 1.00 | 1.000 ± 0.000 |

**What the agents said** (`obs-salt-loyalty-cards`, final answers, abridged):

- shipped, rep 1: "Once you apply each existing environment file and restart its pods, logs, run records, and
  traces will use HMAC-SHA256 card identifiers". No command is named.
- shipped, rep 3: "add a distinct random value ... to `.env.staging` and `.env.prod`, apply the
  Secret, then restart/deploy the pods". It had also edited `auth.py`, `README.md` and the tests.
- best, rep 1: gives `graph-agents-cli secrets apply --env staging`, `graph-agents-cli deploy
  --restart --env staging`, and the same two commands for `prod`, in a code block.

**Claude Code, the same head-to-head** (`acceptEdits`, 3 repetitions, 07:51-07:59): both bodies
scored 18/18 val and 6/6 test.

- Val soft was 0.961 ± 0.005 for the best body and 0.975 ± 0.022 for the shipped one. The
  differences are in non-mandatory checks of `obs-log-level-prod` and `obs-salt-local-dev`, in
  both directions.
- Every session ran in `acceptEdits`. Permission denials: 7 for the best body, 13 for the shipped
  one.
- API-equivalent: $3.96 and $4.92.

**The proposed final text on Codex** (val, 3 repetitions, 07:51-07:55): 18/18 hard. Soft per rep
was 0.958, 0.883 and 0.925, a mean of 0.922. `obs-salt-loyalty-cards` passed 3 of 3. It was
measured before a whitespace-only rewrap of one paragraph (see the review). Test was not rerun
for it.

## Spend

Prices, per 1M tokens, from https://developers.openai.com/api/docs/pricing, read 2026-09-28:

- `gpt-5.6-terra`, short context: $2.00 input, $0.20 cached input, **$2.50 cache writes**, $12.00
  output;
- `gpt-5-mini` (key probe): $0.25 input, $2.00 output.

The harness priced cache writes as input, $2.00, until `8e0f034`. Codex reports them as
`cache_write_input_tokens`, and here they are 99.9 % of the uncached input. The ledger therefore
has a correction of $4.19 for this step, exact from the stored event streams (8,381,665
cache-write tokens).

| Activity | Codex rollouts | Recorded | With the cache-write correction |
|---|---|---|---|
| Key probe and live isolation probes | 3 (+1 key probe) | $0.09 | $0.10 |
| First training run (stopped after its baseline) | 23 | $3.46 | $3.88, plus a $0.30 estimate for the one session killed mid-run (no usage event) |
| Training | 58 | $7.23 | $8.13 |
| Head-to-head, best and shipped | 48 | $7.01 | $7.88 |
| Proposed text, val | 18 | $2.09 | $2.35 |
| Transfer check, test ([`transfer-r3-codex.md`](transfer-r3-codex.md)) | 12 | $1.22 | $1.37 |
| Transfer check, val | 52 | $7.91 | $8.89 |
| Transfer check, extra repetitions and the round-3b text | 26 | $4.84 | $5.44 |
| **This step** | **237 + 3 probes** | **$33.85** | **$38.34** |

- **Corrections.** The correction is spread pro rata in this table. The ledger holds it as one
  entry, and also an upper-bound correction of $1.93 for the Codex runs of rounds 1-2.
- **Totals.** The skillopt track is $57.73 of its $90 cap. The programme ledger (all tracks) is
  $86.47.
- **Per rollout.** Codex averaged about $0.16 and 39 s of agent time (median 28 s) a rollout.
- **Tokens.** 58.4M input, of which 50.0M were cached and 8.4M were cache writes; 0.56M output.
- **Claude Code on the plan: 63 sessions.** 1 preflight, 14 optimizer sessions (analyst 7, merge
  3, ranking 2, slow update 1, meta skill 1; the largest prompt was 44,620 characters) and 48
  head-to-head rollouts ($8.88 API-equivalent).

## Harness findings

| # | Severity | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | blocker | Codex rollouts could not start. `eadd7ee` denied all of `~/.codex`, but the standalone Codex binary lives in `~/.codex/packages`, and `codex exec` re-executes it inside the sandbox. | First live probe: "fs sandbox helper failed ... execvp() of '.../.local/bin/codex' failed: Operation not permitted"; `isolation.codex_config_toml` | Fixed in `540f89d` (`codex_readable_install` opens only `~/.codex/packages`; test); verified with the sandbox and a live session |
| 2 | major | Transcript checks split shell lines inside quotes, so a quoted search pattern became a "command". This is a false negative, and it is biased against the salt edit, whose natural search patterns name `secrets apply` and `deploy`. | First run's baseline, `obs-langsmith-key-rotated` rep 2; `gac_skillopt/verify.py` `command_segments` | Fixed in `9f8aaa1` (quote-aware split, here-documents and comments dropped, keywords stripped; tests); 1 of 2,980 historical checks changes |
| 3 | major | Codex cache writes were priced as plain input ($2.00, against $2.50), and `parse_codex` dropped `cache_write_input_tokens`. Every Codex rollout was recorded about 12 % low. | Pricing page 2026-09-28; `turn.completed` usage in `events.jsonl`; `gac_skillopt/budget.py` `usd` | Fixed in `8e0f034` (test). Ledger corrected: +$4.19 (this step, exact) and +$1.93 (rounds 1-2, upper bound) |
| 4 | major | The in-run ledger check ignored the track cap: `Ledger.check` ran `spend.py check` without `--track`. A 429 `insufficient_quota` was retried, scored 0 and the run went on, so every later rollout was zeroed at $0. | `gac_skillopt/budget.py` `Ledger.check`; round 2's "no credits remaining" rollouts | Fixed in `696c710` (the track is passed; `AgentRun.quota_error` raises `BudgetStop` without a retry; 3 tests, which fail without the fix) |
| 5 | minor | Two Codex `baseline run` invocations started in the same second shared one scratch `CODEX_HOME`. The first to finish deletes the login under the other. | `baseline.py` `cmd_run` named it by the second | Fixed in `e95c8dd` (`codex_home_name`; test) |
| 6 | minor | The fact-check's size cap (1.25 × shipped) binds hard on a short skill. After one accept at +20.7 %, all four later candidates were refused before any rollout, so epoch 2 evaluated nothing. | Steps 2-4 at 13,417, 13,311 and 13,121 characters (cap 13,097); slow update at 16,293 | Open. Options: a floor on the cap (for example 1.25 × shipped or shipped + 4,000 characters, whichever is larger), or let a step replace text instead of only adding it |
| 7 | minor | Rollouts can read the bundled shipped skills in the scratch CLI install (`uv-tools/graph-agents-cli/.../graph_agents_cli/skills/data/*/SKILL.md`), whose path `graph-agents-cli info` prints. A rollout of a candidate body could read the shipped body of the same skill. | One Codex rollout (`transfer-val-shipped` rep 1, `scaffold-guidance-mixed-team`, a shipped-text run) listed and read `skills/data/.../SKILL.md`; no candidate-body run did | Open: deny reads of `skills/data` in the scratch install after checking that no CLI command a task needs reads it |
| 8 | minor | `~/.codex/packages` (the Codex install, no credentials) is readable to Codex rollouts since finding 1's fix. | 4 rollouts searched it for `SKILL.md` and found none | Accepted: the narrowest opening that lets Codex run |
| 9 | minor | The salt tasks check only `app/` (and `pyproject.toml`) for unrequested changes. The shipped skill's Codex agents also rewrote `README.md`, `.env.example` and unit tests, and no check saw it. | Head-to-head file changes, above | Open (benchmark): add those paths to `code-untouched` for new variants. Test tasks stay frozen |
| 10 | minor (template) | The generated project's integration tests leave `langgraph dev` servers running when run inside the Codex sandbox. The harness stopped them in 7 rollouts; 2 more left the agent's own `uvicorn`. | `leftovers` in 9 results: 7 are `langgraph dev` from `tests/integration/test_approvals_server.py` fixtures | Open. Root cause not verified; a candidate is that `_stop` (`test_approvals_server.py:353-366`) does not catch a `PermissionError` from `os.killpg` |

## Cleanup and checks

- **Workspaces, processes, logins:**
  - `/private/tmp/gac-x-skillopt` is empty.
  - No listener is on ports 22150-22199, and no `gac_skillopt`, `codex exec` or rollout process
    remains.
  - No `auth.json` is left under the scratch `codex/`.
- **Key scan** (`/usr/bin/grep -rlI -F -f <key>`): 0 files. It covered:
  - this step's scratch (without the uv caches and the CLI install);
  - `tools/skillopt/`;
  - the session's task outputs;
  - the 49 new Claude Code temp directories of this step's rollouts.

  No new `~/.claude/projects/-private-tmp-gac-x-skillopt-*` directory was created.
- **Denied content in rollout commands.** Rollout commands that reached outside the workspace
  were searched. None read content below a denied directory. What was read:
  - `~/.codex/packages` (finding 8);
  - the scratch CLI install (finding 7; readable by design, the rollouts execute it);
  - two Claude rollouts ran `find /` for CLI files, which lists names only.
