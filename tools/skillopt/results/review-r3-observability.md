# Human review of the round-3b SkillOpt candidate (observability, Codex)

Date: 2026-09-28. Branch `experiments/skillopt`. This page reviews the best body of the Codex
SkillOpt run on the observability skill ([`train-r3-observability.md`](train-r3-observability.md))
against the shipped skill. It uses the method of [`review-r2.md`](review-r2.md).

**Nothing has been written back.** `skills/` and `src/graph_agents_cli/skills/data/` are unchanged
on this branch. The complete proposed `SKILL.md` is at the end of this page.

## What was reviewed

| Text | Body characters | Body sha256 | `SKILL.md` sha256 |
|---|---|---|---|
| Shipped (the training's initial skill) | 10,478 | `930dde3bea64927b1f52c4407bb78feb22660049ddb95abc11ef1865009129f9` | `af66b46cb02f8668af445f3eb0cdf8e795f2ecd2db1be3fd6848f0c1a3c3fd92` |
| Optimizer's best (step 1) | 12,649 (+20.7 %) | `50568f615a55b88e21098244a77d810d394b92cd63248b984cd6ba7c7f3172c0` | `23b35672afcb22c4bed3d7e9ca04682b991d29de24e991336b9714d3c1147ebf` |
| **Proposed final** | 12,950 (+23.6 %) | `530dfe5e35839d91f05d8bbfa525bf0527db0c5860ac1bc5e5975b0e7238553d` | `e8fff7d5f407a334616bdba4409cad7b0dfbd06108c92480ff64d7034c9e5ae1` |

- **Body and frontmatter.** The "body" is `SKILL.md` without its frontmatter (`tasks.split_skill`).
  The frontmatter is the shipped file's, unchanged. Rendering the shipped body with it gives back
  the shipped file byte for byte.
- **Fact-check.** All three bodies pass `factcheck.check_candidate`: sections kept, no unknown
  command or flag, and within the size cap of 13,097 characters. The proposed text leaves 147
  characters of room.
- **Provenance.** It comes from step 1's `step_record.json`, `merged_patch.json`,
  `ranked_edits.json`, `edit_apply_report.json` and `patches/*.json` in the run's scratch.
- **Line numbers.** Source line numbers are at `e95c8dd`; the CLI sources are unchanged since
  `b35b246`. `app/...` paths are the generated project's, under
  `src/graph_agents_cli/scaffold/agents/langgraph/`; chart paths are under
  `src/graph_agents_cli/scaffold/deployment_targets/kubernetes/python/`. `SKILL.md` line numbers
  are the proposed file's.

**How the hunks are classified** (as in round 2):

- **targeted fix**: a failure-derived edit that addresses the round-1 target, here the salt
  procedure marked as configuration-only;
- **success-derived addition**: written by success-only reflection from trajectories that already
  passed;
- **risky**: the hunk, or part of it, has a factual error, wording copied from the benchmark, a
  misplacement, or a rule that could block legitimate work.

## Summary

| Hunk | Class | Provenance | Verdict for write-back |
|---|---|---|---|
| O1 New section "Procedure: salt the hashed principal id" | **targeted fix**, one clause imprecise | step 1, failure patch, support 1 (`obs-salt-phone-numbers`: "run your normal secret apply and rollout", and edits to the chart template and README) | keep; rewrap, drop "until retention removes them", point to it from "Hashed principal id" |
| O2 "enable tracing": commands still to run | success-derived, **risky** (scope) | step 1, success patch, support 2 (three-rollout success minibatch) | keep; separate the one-environment rule from the "not deploying" condition |
| O3 "Locally": edit only `.env`, OTLP selection, capture level | success-derived, **risky** (one imprecise reason) | step 1, success patch, support 1 (`obs-local-otlp`) | keep; state what the app actually does with the SDK switches |
| O4 Pointer from "Hashed principal id" to the procedure | optimizer-proposed, cut by the budget | step 1, success patch (`minibatch_succ_000`), ranked out (budget 3) | add, shortened |

**Measured effect.** All numbers are fresh rollouts, 3 repetitions; "mean ± sd" is over the
repetitions. Codex is `gpt-5.6-terra`; Claude Code is `sonnet` in `acceptEdits`.

| Text | Harness | val hard | val soft | test hard | test soft | `obs-salt-loyalty-cards` | test `obs-principal-salt` names `secrets apply` |
|---|---|---|---|---|---|---|---|
| shipped | Codex | 0.83 ± 0.00 (15/18) | 0.85 ± 0.01 | 1.00 (6/6) | 0.90 | 0 of 3 | 0 of 3 |
| optimizer's best | Codex | 1.00 ± 0.00 (18/18) | 0.93 ± 0.06 | 1.00 (6/6) | 1.00 | 3 of 3 | 3 of 3 |
| **proposed final** (val only) | Codex | 1.00 ± 0.00 (18/18) | 0.92 ± 0.04 | not run | not run | 3 of 3 | not run |
| shipped | Claude | 1.00 (18/18) | 0.975 ± 0.022 | 1.00 (6/6) | 1.00 | 3 of 3 | 3 of 3 |
| optimizer's best | Claude | 1.00 (18/18) | 0.961 ± 0.005 | 1.00 (6/6) | 1.00 | 3 of 3 | 3 of 3 |

- **The gain is on the target task.**
  - Every fresh Codex measurement of `obs-salt-loyalty-cards` together gives: shipped 1 of 9
    (the stopped run's baseline, the training baseline and the head-to-head), against 6 of 6 for
    the best and proposed texts. Fisher p = 0.001.
  - Val hard as a whole, 15/18 against 18/18, gives p = 0.23.
- **The frozen test split** passes its mandatory checks under both texts. Its non-mandatory
  `next-step` check (the final answer names `secrets apply`) moves from 0 of 3 to 3 of 3.
- **Side effect: the edit narrows what Codex changes.** In the 9 salt rollouts of each text:
  - shipped: `README.md` 5, `.env.example` 4, `app/app_utils/auth.py` 2, unit tests 2, a spec
    file 1;
  - best: the manifest 8, `.env` 3 (the local task, which needs it), a spec file 1, nothing else.

  No check scored the README, `.env.example` or test edits (training report, finding 9).
- **Claude is saturated** on this skill: no difference, and no regression.
- **Only O1 is backed by a failure the optimizer saw.** O2 and O3 came from passing trajectories,
  and no val task isolates their effect.

## Diff: the optimizer's best body against the shipped body

```diff
--- shipped/observability
+++ best/observability
@@ -139,9 +139,32 @@
 4. Send one request with `graph-agents-cli run --url ... "hello"` and find the trace by
    `thread_id` / `run_id` from the `message.end` event.
 
-Locally: set `TRACING_ENABLED=true` and either key or endpoint in `.env`; `playground` and `run`
-pick it up.
+When told not to deploy, or when the user runs cluster commands, change only the target
+environment's `values-<env>.yaml` or `.env.<env>`; `values.yaml` and other environments stay as
+they are. End your answer with the exact commands still to run, and do not run them yourself:
+- `graph-agents-cli secrets apply --env <env>`, whenever a value goes into `.env.<env>` (even one
+  the user fills in).
+- Then the deploy (`deploy --restart --env <env>` after a Secret-only change).
 
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
+
 ## Troubleshooting
 
 | Symptom | Fix |
```

## Hunks

**O1, "Procedure: salt the hashed principal id": targeted fix.**

This is the round-1 target. It came from one failing train rollout, `obs-salt-phone-numbers`, whose
final answer never named the commands. The analyst's summary also notes "the agent also edited the
chart template and README when the change needed only the manifest's `secrets.keys`". It lists the
four steps, says the change is configuration only, and tells the agent to hand over the exact
commands. That is all the round-1 target asked for.

What is risky or rough in it:

1. **Placement.** The optimizer added a new `##` section after "Procedure: enable tracing"
   (`insert_after "pick it up."`), not in "Hashed principal id" (`SKILL.md:81-93`), where the salt
   is introduced.
   - A second procedure section next to the first is a reasonable home.
   - But an agent that reads only "Hashed principal id" is not sent there. O4 adds the pointer.
2. **"until retention removes them" is not precise.**
   - Run records are purged by `RETENTION_DAYS` only with the app's own store. It purges threads
     idle longer than that (`app/app_utils/chat.py:62`).
   - Traces and logs follow whatever the destination keeps.
   - The shipped text and `Principal.hashed_id()`'s docstring say only that new hashes no longer
     match older ones (`app/app_utils/auth.py:149-150`). The proposed text says that.
3. **Wording from the benchmark.**
   - "A phrase such as 'apply your secrets and roll out' is not enough" paraphrases the failing
     answers ("run your normal secret apply and rollout", "apply each Secret and restart").
   - "(emails, phone numbers, account numbers)" names the train task's id type (phone numbers).
   - Both are generic enough to keep: a user who runs the cluster commands needs them verbatim,
     and phone numbers are a common guessable id.
   - Nothing names a val or test task's project, prompt or values. The frozen test task's id type
     (email addresses) was already in the shipped text.
4. **Long lines.** Steps 1-4 and the last paragraph were single lines of 121 to 308 characters.
   They are rewrapped to the file's 100 columns.

**O2, "enable tracing": the commands still to run. Success-derived, risky (scope).**

It came from the success minibatch of three passing trajectories. The analyst's summary says
"trajectory 2 did this and passed; trajectory 1 omitted the `secrets apply` command and failed the
check". It does two things.

- **The commands still to run.** When the user runs the cluster commands, the answer ends with
  them, and the agent does not run them itself. That is sound, and matches the salt procedure.
- **Which files to change.** "change only the target environment's `values-<env>.yaml` or
  `.env.<env>`" is sound for a change to one environment. The problem is its condition: it hangs
  on "When told not to deploy, or when the user runs cluster commands".
  - Read literally, the file rule applies only when the agent does not deploy.
  - Read the other way, it could stop a change that belongs in `values.yaml` for every
    environment.
  - The proposed text states the file rule for a change to one environment, and keeps the
    commands rule under its condition.

**O3, "Locally". Success-derived, risky (one imprecise reason).**

It came from `obs-local-otlp`, which passed. Most of it is right:

- edit only `.env`;
- leave `LANGSMITH_API_KEY` unset to select OTLP (`app/app_utils/telemetry.py:345-348`);
- keep `TRACE_CAPTURE=metadata` unless the user asked for `full`;
- leave the other entries alone.

Its reason for not adding the SDK's own variables, "the app reads only the variables in the table
above", is not quite true:

- The OTLP exporter also reads the `OTEL_EXPORTER_OTLP_*` headers (`telemetry.py:430`).
- The app sets `LANGSMITH_TRACING` itself: to `true` for the LangSmith path (`telemetry.py:373`),
  and to `false` for both SDK switches when tracing is off (`telemetry.py:337-341`).
- The advice itself is sound. On the OTLP path the app does not reset the SDK switches, so a
  stray `LANGSMITH_TRACING=true` there reaches LangChain unchecked.

The proposed text states the mechanism instead.

**O4, a pointer from "Hashed principal id": optimizer-proposed, cut by the budget.**

- **What the optimizer proposed.** Step 1's larger success patch replaced "Changing the salt
  changes every hash." with a rotation recipe. The ranking kept 3 of 5 edits, so it was cut.
- **What the proposed text adds.** A shorter form: the hash-change consequence, and "To set the
  salt or rotate a leaked one, follow 'Procedure: salt the hashed principal id' below (no code or
  values change)". With the other changes, the proposed text is 301 characters longer than the
  best body and stays under the cap.

### Claims checked against the code

| # | Claim (hunk) | Verdict | Evidence |
|---|---|---|---|
| 1 | The hashed id is HMAC-SHA256 keyed with `PRINCIPAL_HASH_SALT` when it is set, and plain sha256 otherwise; "the app already uses HMAC ... whenever the variable is set" (O1) | true | `app/app_utils/auth.py:103` (`PRINCIPAL_HASH_SALT_ENV`), `:143-156` (`hashed_id`; a blank value counts as unset) |
| 2 | Only keys listed in `secrets.keys` reach the Secret, so the salt must be listed (O1) | true | `src/graph_agents_cli/secrets/_apply.py:21`, `:127` ("Keep only allow-listed, non-empty values"); `cmd_secrets.py:95` (`secrets apply`: "from the allow-listed keys of an env file") |
| 3 | `graph-agents-cli secrets apply --env <env>` reads `.env.<env>` (O1, O2) | true | `src/graph_agents_cli/secrets/cmd_secrets.py:62-68` (`--env` required; `--env-file` "defaults to .env.<env>") |
| 4 | `graph-agents-cli deploy --restart --env <env>` restarts the pods after a Secret change (O1, O2) | true | `src/graph_agents_cli/deploy/cmd_deploy.py:112-116` ("Rollout-restart the Deployment (after a Secret rotation) and wait for the new pods") |
| 5 | Pods read a changed Secret only when they start (O1) | true | the chart injects the Secret as environment variables (`envFrom: secretRef`, `deployment/helm/{{cookiecutter.project_name}}/templates/deployment.yaml:66-69`), which are read at container start |
| 6 | "Every hash changes once" (O1) | true | `auth.py:149-150` ("changing the salt changes every hash, so new hashes no longer match older logs and run records") |
| 7 | "... keep the old unsalted hashes until retention removes them" (O1) | **imprecise** | `RETENTION_DAYS` purges idle threads (and their run records) in the app's own store (`app/app_utils/chat.py:62`); traces and logs follow the destination's retention. Dropped |
| 8 | Chart layout: `values.yaml` plus `values-<env>.yaml` per environment (O2) | true | `deployment/helm/{{cookiecutter.project_name}}/values{,-dev,-staging,-prod}.yaml` |
| 9 | With tracing on, OTLP is chosen when `LANGSMITH_API_KEY` is unset (O3) | true | `app/app_utils/telemetry.py:345-348` |
| 10 | "the app reads only the variables in the table above" (O3) | **imprecise** | the OTLP exporter also reads `OTEL_EXPORTER_OTLP_*` headers (`telemetry.py:430`); the app sets `LANGSMITH_TRACING` itself (`:337-341`, `:373`) |
| 11 | `python -c "import secrets; print(secrets.token_hex(32))"` gives a long random value (O1) | true | Python standard library; 64 hex characters |

### Changes made in the proposed final text

Each change is labelled. The optimizer's other text is kept word for word; only long lines are
rewrapped.

| Change | Why | Kind |
|---|---|---|
| O1 steps 1-4 and the last paragraph rewrapped to 100 columns | lines of 121 to 308 characters (O1 risk 4) | format |
| O1 step 4: "Every hash changes once. Older traces, logs and run records keep the old unsalted hashes until retention removes them." → "Every hash changes once: older traces, logs and run records keep the old hashes and no longer match new ones." | claim 7 | factual precision |
| "Hashed principal id": "Changing the salt changes every hash." → "... so older logs and run records no longer match new ones. To set the salt or rotate a leaked one, follow "Procedure: salt the hashed principal id" below (no code or values change)." | O1 risk 1 (placement), O4 | consistency (optimizer-proposed, shortened) |
| O2: "When told not to deploy, or when the user runs cluster commands, change only the target environment's ... End your answer with ..." → "For a change to one environment, edit only that environment's ... When told not to deploy, or when the user runs cluster commands, end your answer with ..." | the file rule no longer depends on who deploys | scoping |
| O3: "Do not add the LangChain/LangSmith SDK's own tracing variables (the app reads only the variables in the table above)" → "Do not add the LangSmith SDK's own switches (`LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2`): `TRACING_ENABLED` is the switch, and the app sets `LANGSMITH_TRACING` itself when LangSmith is the destination." | claim 10 | factual correction |

## Not changed, for the owner to decide

1. **The rejected candidates never reached a rollout.** The fact-check's size cap (1.25 × a
   10,478-character body) refused steps 2-4 and the slow update before any val rollout (training
   report, finding 6). Two are worth a human look; neither was measured:
   - **Step 4, salt rotation.**
     - When the key is already in `secrets.keys`, change only the `PRINCIPAL_HASH_SALT` line of
       `.env.<env>`.
     - Generate the value yourself.
     - Do not print it in the answer.

     O4's pointer covers the procedure. "Do not print the value" is not in the proposed text.
   - **Steps 2 and 3, LangSmith.** Check that `LANGSMITH_API_KEY` is in `secrets.keys`. For a
     self-hosted LangSmith, set `env.LANGSMITH_ENDPOINT` in `values-<env>.yaml`, because the
     `tracing` block has no endpoint field. This matches the round-2 Claude failure on
     `obs-langsmith-self-hosted` (`val-baseline-r2.md`).

   Raising the cap for short skills (finding 6) and rerunning would test them. On Codex that
   costs about $10.
2. **Local runs.** The procedure speaks of `.env.<env>` and deployed environments. For local runs
   the salt goes in `.env` (the `obs-salt-local-dev` task). The proposed text passed that task 3
   of 3 on Codex, so no wording is proposed.
3. **Where it lands.** The owner approved the workflow and scaffold write-back for `v0.3` (round-3b
   decision 2). An observability write-back would go the same way: both copies, byte-identical,
   with a CHANGELOG line that credits the SkillOpt experiment.
4. **The benchmark gap** (training report, finding 9). The salt tasks do not check `README.md`,
   `.env.example` or the tests. So the proposed text's narrower changes, the second benefit
   above, are measured here only by counting file changes.

## Confirmation of the final text

The proposed text was run on Codex val, 3 repetitions in parallel (18 rollouts, 07:51-07:55,
$2.35 with the cache-write correction):

- **Val:** hard 18/18. Soft per repetition was 0.958, 0.883 and 0.925 (0.922 ± 0.04), against
  0.933 ± 0.06 for the optimizer's best.
- **`obs-salt-loyalty-cards`:** 3 of 3, each naming `secrets apply --env` and `deploy --restart`
  for staging and prod.
- **Files changed in the salt rollouts:** only the manifest (4) and `.env` (3).
- **One whitespace change after measurement.** After the run, the "Hashed principal id"
  paragraph that O4 extends was rewrapped to 100 columns. That is whitespace only: the text's
  words are identical, and the body length is unchanged at 12,950 characters. The measured body
  had sha256 `dbfb06fd…`.
- **Not rerun:** test, and Claude. On Claude the optimizer's best body showed no regression.

## Write-back, once approved (not done)

1. Replace the body of `skills/graph-agents-cli-observability/SKILL.md` with the proposed text on
   `v0.3`, keeping the frontmatter. Copy the file to
   `src/graph_agents_cli/skills/data/graph-agents-cli-observability/SKILL.md`; the two copies must
   stay byte-identical.
2. Add a CHANGELOG entry under Unreleased that credits the SkillOpt experiment (Codex rollouts).
3. Run the fast suite, `ruff`, and `mkdocs build --strict` if the site quotes the skill.

## Proposed `SKILL.md` (complete file)

The file below round-trips exactly (sha256 `e8fff7d5f407a334…`): its frontmatter is the shipped
file's, and its body is the proposed final body.

````markdown
---
name: graph-agents-cli-observability
description: >
  This skill should be used when the user wants to "set up tracing",
  "enable LangSmith", "send traces to our collector", "monitor my agent",
  "debug production traffic", "see what the agent sent to the model", "log
  prompts", or needs guidance on observability for a graph-agents-cli
  project. Covers the TRACING_ENABLED opt-in, the TRACE_CAPTURE metadata
  versus full policy, LangSmith versus OTLP, the hashed principal id, run
  records, and what is never captured by default. Part of the
  graph-agents-cli skills suite. Do NOT use for deployment
  (graph-agents-cli-deploy) or agent code (graph-agents-cli-langgraph-code).
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
---

# Observability guide

> **Tracing is off unless `TRACING_ENABLED=true`.** No exporter is configured and no LangSmith
> client is created otherwise. Setting `LANGSMITH_API_KEY` alone does not enable tracing.
> Enabling tracing to a hosted destination is an egress decision the user makes explicitly.

## Reference files

| File | Contents |
|---|---|
| `references/langsmith.md` | LangSmith destination: variables, self-hosted endpoint, projects, what the capture policy does to LangSmith runs, `eval submit` |
| `references/otel.md` | OTLP destination: LangChain OpenTelemetry instrumentation, collector configuration, span attributes, in-cluster collectors |

---

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `TRACING_ENABLED` | `false` | the opt-in; nothing is exported while false |
| `TRACE_CAPTURE` | `metadata` | `metadata` or `full`; applied identically to LangSmith, OTLP, and run records |
| `LANGSMITH_API_KEY` | (Secret) | when set with tracing enabled, traces go to LangSmith |
| `LANGSMITH_PROJECT` | project name | LangSmith project |
| `LANGSMITH_ENDPOINT` | LangSmith cloud | override for a self-hosted LangSmith |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (chart) | OTLP fallback used when tracing is enabled and no LangSmith key is set |

Chart values: `tracing: { enabled, capture, otlpEndpoint, langsmith: { project } }` render these
into `env:`; the key comes from the Secret. Locally, `.env` sets `TRACING_ENABLED=false` and
`TRACE_CAPTURE=metadata`.

## Destination selection

```
TRACING_ENABLED=false            -> nothing
TRACING_ENABLED=true + LANGSMITH_API_KEY   -> LangSmith (LANGSMITH_PROJECT, LANGSMITH_ENDPOINT)
TRACING_ENABLED=true, no LangSmith key     -> OTLP to OTEL_EXPORTER_OTLP_ENDPOINT via LangChain OpenTelemetry instrumentation
```

The disconnected profile allows only the OTLP path to an in-cluster collector, or tracing off.

## Capture policy

| `TRACE_CAPTURE` | Captured | Never captured |
|---|---|---|
| `metadata` (default) | span structure and timing; model and provider names; token counts; tool **names**; error **types** and HTTP status codes; identifiers (`thread_id`, `run_id`, hashed principal id, agent version) | prompt text, completion text, tool arguments, tool results, error messages, the client's `/chat` metadata |
| `full` | everything in `metadata` plus prompts, completions, tool arguments, tool results, full error messages, and the client's `/chat` metadata (as `client_metadata`) | |

Clients can never overwrite trace ids through `/chat` metadata, and that metadata is never
written into checkpoints (it is kept in the run record).

Enabling `full` against a hosted destination sends user content off-network. The consuming
project must decide that explicitly (and publish a truthful privacy notice) before you set it. Do
not switch to `full` on your own to debug; ask, and prefer reproducing locally with
`graph-agents-cli run -v`, which prints the events to your terminal without exporting anything.

The same policy governs the chat API: `tool.call` events omit `args` for a caller who is not the
thread's owner under `metadata`.

## Hashed principal id

Traces, logs, run records, and eval traces record `Principal.hashed_id()` (the first 16 hex
characters of sha256 of the policy's `Principal.id`, or of HMAC-SHA256 keyed with
`PRINCIPAL_HASH_SALT` when that secret is set), never the raw id. Audit and thread ownership use
the same identity, so a support engineer can correlate a trace with a conversation without the
trace revealing who the user is. Set `PRINCIPAL_HASH_SALT` (and add it to `secrets.keys`) when ids
are guessable, such as email addresses: without a salt anyone holding a trace can confirm a guess
by hashing it. Changing the salt changes every hash, so older logs and run records no longer match
new ones. To set the salt or rotate a leaked one, follow "Procedure: salt the hashed principal id"
below (no code or values change). Under `shared-bearer` every caller is `shared`. Under
`langgraph-server`, runs started through the server's native API carry the raw id in checkpoint
metadata (the server injects it); `/chat` runs carry only the hash.

## Run records

Besides traces, the app writes one run record per `/chat` call: run id, thread id, hashed
principal, model, token counts, latency, status, error type, the client's `/chat` metadata;
payload (prompt, completion, tool I/O) only under `TRACE_CAPTURE=full`. The record is written
when the run starts (`running`) and updated when it ends: `ok`, `step_limit` (the run reached
`RECURSION_LIMIT` and ended with a reply saying so), `error`, `timeout`, `cancelled` (the client
left) or `interrupted` (the run lost its lease on the thread, or its process died: a crash, an
OOM kill, a lost node, a rollout that ran out of grace). Records a dead process left `running`
are marked `interrupted` (`error_type` `ProcessLost`) by any replica within about a minute of
the run's 30 s lease expiring, so crashes can be counted and audited.

- `CHECKPOINTER=postgres`: durable rows in the agent-owned database (`runs` table, created
  under an advisory lock).
- `CHECKPOINTER=memory`: in-process, served to the `/playground` page while the process lives,
  lost on restart. Local development needs no database.
- Under `langgraph-server`, the app keeps its records in an `agent_runs` table in the server's
  Postgres (`DATABASE_URI`), beside the server's own Runs API.
- Retention: `RETENTION_DAYS=N` deletes threads idle for more than N days with their checkpoints
  and run records, in an hourly best-effort pass on every replica (0, the default, keeps
  everything). `DELETE /threads/{thread_id}` deletes one thread and its records on request.
- Evaluation never depends on run records; `eval generate` writes trace files.

## Logs, metrics and health

- **Logs** are JSON lines by default outside `APP_ENV=dev` (`LOG_FORMAT=json|text`, `LOG_LEVEL`),
  each with the request id (`X-Request-ID`, echoed to the client), run id, thread id and hashed
  principal. Client-facing errors carry an `error_id`; the exception is logged under that id. The
  app does not log credentials, messages or tool arguments.
- **Metrics:** `GET /metrics` serves Prometheus text (`METRICS_ENABLED`, default true):
  `http_requests_total` and `http_request_duration_seconds` (by method, route, status),
  `agent_runs_total` (by status; `interrupted` also counts the dead processes' runs a replica
  closed, once across replicas), `agent_active_runs`, `agent_run_duration_seconds`,
  `agent_tokens_total`, `agent_database_up` (0 while the database is known to be unreachable).
  It is unauthenticated unless `METRICS_TOKEN` is set (then the scraper
  sends `Authorization: Bearer <token>`), and the chart never publishes it on the Gateway or
  Ingress. Scrape it with `metrics.serviceMonitor.enabled` (Prometheus Operator; add
  `metrics.serviceMonitor.bearerToken.enabled` when `METRICS_TOKEN` is set, so it sends the
  token from the app Secret) or `metrics.scrapeAnnotations` (annotations carry no token: put
  it in that Prometheus's scrape job). Under `langgraph dev` the server's own `/metrics` answers instead;
  the server image disables it so the app's is served.
- **Health:** `GET /health` is liveness (the process answers); `GET /ready` is readiness (the
  database is set up and answers within 2 s, else 503). A pod started during a database outage
  stays up and unready, and is ready again seconds after the database is. During an outage
  requests get 503 within a few seconds and each logs one WARNING line (`Database unavailable
  (error_id=...)`), without a traceback. Useful alerts: `/ready` failing,
  `agent_database_up == 0`, a rising `agent_runs_total{status!="ok"}` (notably `interrupted`
  and `step_limit`), `agent_active_runs` near capacity.

## What is never captured by default

- Prompt and completion text, tool arguments and results, error messages (only under `full`).
- Raw principal ids (except the langgraph-server native-API case above), session cookies,
  session tokens, bearer keys and `attributes["credentials"]` (never, under any setting).
- External API payloads beyond what a tool returns into the trace (governed by `full`).
- Anything at all while `TRACING_ENABLED=false`.

## Procedure: enable tracing for an environment

1. Confirm the destination and capture level with the user (egress decision).
2. LangSmith: put `LANGSMITH_API_KEY` in `.env.<env>`, run `graph-agents-cli secrets apply --env
   <env>`; set `tracing.enabled=true`, `tracing.capture`, `tracing.langsmith.project` in
   `values-<env>.yaml`.
   OTLP: set `tracing.enabled=true` and `tracing.otlpEndpoint=http://<collector>:4318` in
   `values-<env>.yaml`; no key.
3. Deploy per the project's mode (`/graph-agents-cli-deploy`); in argocd mode the values change
   is a PR. After a Secret change alone, `deploy --restart --env <env>`.
4. Send one request with `graph-agents-cli run --url ... "hello"` and find the trace by
   `thread_id` / `run_id` from the `message.end` event.

For a change to one environment, edit only that environment's `values-<env>.yaml` or
`.env.<env>`; `values.yaml` and the other environments stay as they are. When told not to deploy,
or when the user runs cluster commands, end your answer with the exact commands still to run, and
do not run them yourself:
- `graph-agents-cli secrets apply --env <env>`, whenever a value goes into `.env.<env>` (even one
  the user fills in).
- Then the deploy (`deploy --restart --env <env>` after a Secret-only change).

Locally: edit only the project's `.env` (no chart values, no code). Set `TRACING_ENABLED=true` plus
either `LANGSMITH_API_KEY` or `OTEL_EXPORTER_OTLP_ENDPOINT=<collector URL>`; for OTLP leave the
LangSmith key unset so the OTLP path is selected. Keep `TRACE_CAPTURE=metadata` unless the user
explicitly asked for `full`. Do not add the LangSmith SDK's own switches (`LANGSMITH_TRACING`,
`LANGCHAIN_TRACING_V2`): `TRACING_ENABLED` is the switch, and the app sets `LANGSMITH_TRACING`
itself when LangSmith is the destination. Leave the other `.env` entries (model provider,
API key) as they are. `playground` and `run` pick the settings up. If the collector is not running
yet, say so in your answer; the configuration is still correct.

## Procedure: salt the hashed principal id

Use this when principal ids are guessable (emails, phone numbers, account numbers).

1. Add `PRINCIPAL_HASH_SALT` to `secrets.keys` in `graph-agents-cli-manifest.yaml` and keep the
   existing keys. This is configuration only. The app already uses HMAC for the hash whenever the
   variable is set, so do not change code, chart templates or `values-*.yaml`, and never put the
   value in a values file.
2. The user puts a long random value (for example
   `python -c "import secrets; print(secrets.token_hex(32))"`) in `.env.<env>` and keeps it stable.
3. `graph-agents-cli secrets apply --env <env>`, then `graph-agents-cli deploy --restart --env
   <env>`. Pods read a changed Secret only when they start. Repeat both steps for every deployed
   environment.
4. Every hash changes once: older traces, logs and run records keep the old hashes and no longer
   match new ones.

When the user runs cluster commands themselves, the final answer must list these commands
verbatim, with the real environment name substituted (for example `--env prod`). A phrase such as
"apply your secrets and roll out" is not enough.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No traces appear | `TRACING_ENABLED` is not `true` (the key alone does nothing); check `GET /health` and the pod env |
| Traces in LangSmith but empty prompts | expected under `metadata`; `full` is an explicit decision |
| OTLP exporter connection refused | endpoint must be reachable from the pod; use the collector's Service DNS and port 4318 (HTTP) |
| Traces from `eval generate` mixed with production | use `LANGSMITH_PROJECT` per environment; eval traces are files unless tracing is on |
| Need to know who a trace belongs to | correlate `hashed_id` with the client application's session log; the raw id is never in the trace |

## Not covered by this skill

- Deploying the values or Secret changes: `/graph-agents-cli-deploy`.
- Writing nodes or tools that emit custom spans: `/graph-agents-cli-langgraph-code` (keep
  prompt text out of logs).
- The eval gate and results files: `/graph-agents-cli-eval`.
- Cluster-wide metrics collection, logging stacks, dashboards, alerting rules: platform tooling
  outside the CLI (the app exposes `/metrics` and JSON logs for them).
- Self-hosted LangSmith installation (point `LANGSMITH_ENDPOINT` at one that exists).

## Migration note

Compared with google-agents-cli: Cloud Trace, Cloud Logging, prompt-response logging to GCS and
BigQuery, and the BigQuery Agent Analytics plugin are gone, and tracing is no longer always on.
LangSmith or OTLP behind `TRACING_ENABLED` with the `TRACE_CAPTURE` policy replaces them.
````
