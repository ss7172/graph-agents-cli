---
name: graph-agents-cli-eval
description: >
  This skill should be used when the user wants to "run an evaluation",
  "evaluate my agent", "write an eval dataset", "add an eval case",
  "analyze eval failures", "compare eval results", "set a quality
  threshold", "why did eval exit 1", or "upload evals to LangSmith".
  Covers the enforceable eval gate (one rule, case statuses, exit codes),
  the dataset schema, deterministic expect checks, judge and quality
  metrics, the local-versus-disconnected distinction, and `eval submit`.
  Applies to any graph-agents-cli project. Do NOT use for agent code
  (graph-agents-cli-langgraph-code), deployment (graph-agents-cli-deploy),
  or scaffolding (graph-agents-cli-scaffold).
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
---

# Agent evaluation guide

> **Requires:** `graph-agents-cli` (`uv tool install git+https://github.com/ss7172/graph-agents-cli`).

> **Scaffolded project?** `tests/eval/datasets/basic-dataset.json` and
> `tests/eval/eval_config.yaml` already exist. Start with `graph-agents-cli eval run` and iterate.

## Reference files

| File | Contents |
|---|---|
| `references/dataset_schema.md` | Dataset, trace, and results JSON schemas with examples and common mistakes |
| `references/metrics-guide.md` | Every `expect` check, the built-in judges, quality metrics, custom metrics, judge configuration |

---

## The gate rule

> **One rule.** Three things are always mandatory and have no threshold: complete case accounting
> (no `error` or `missing` case), every deterministic check, and every judge metric a case declares
> as mandatory. Only judge metrics explicitly designated as *quality metrics* in
> `tests/eval/eval_config.yaml` (`quality_metrics:` with a per-case `threshold` and an aggregate
> `min_pass_rate`) may pass at an agreed rate below 100 percent. A run passes when all mandatory
> items pass and every quality metric meets its `min_pass_rate`.

> **Case statuses.** Every planned case in the dataset ends in exactly one status: `passed`,
> `failed` (a mandatory check or mandatory judge metric failed), `quality_below_threshold` (all
> mandatory items passed but at least one designated quality metric scored under its per-case
> threshold), `error` (generation or grading raised), or `missing` (no trace was produced or the
> trace lacks a response). `eval run` and `eval grade` print a per-status count and write it to the
> results file.

> **Planned-case accounting.** The gate compares the set of case ids in the dataset with the set
> present in the traces and the set graded. Any id absent from either is `missing`. A results file
> that covers fewer cases than the dataset cannot pass.

> **Exit codes.** `0` when no case is `failed`, `error`, or `missing`, and for every designated
> quality metric the fraction of the cases **scored on that metric** (the cases that declare it and
> reached the judge) that met its threshold is at least its `min_pass_rate`. A case that never
> declared the metric is not counted as a pass; a quality metric no case ran is reported `n/a (no
> case ran it)` and cannot fail the gate. `1` when any case is `failed` or any quality metric misses
> its `min_pass_rate`. `2` when any case is `error` or `missing` (incomplete qualification; a
> quality rate is never computed over an incomplete run). `3` for configuration errors (unknown
> metric, unreachable judge, a quality metric with no threshold, an unknown `prompt_template`
> placeholder). `eval run` returns the worst code of its two stages. CI treats non-zero as a
> failed check.

There is no run-wide `min_pass_rate`. Mandatory controls and case accounting cannot be relaxed by
configuration. **The exit code is the gate; do not "read the scores" and declare success on a
non-zero exit.**

---

## Commands

```bash
graph-agents-cli eval run      [--dataset F] [--url URL] [--concurrency N] [-H ...] [--cookie ...]
                               [--app-name N] [--timeout S] [--config F] [-o F] [--judge-provider P] [--judge-model M] [--judge-timeout S]
graph-agents-cli eval generate [--dataset F] [-o F] [--url URL] [--concurrency N] [-H ...] [--cookie ...] [--app-name N] [--timeout S]
graph-agents-cli eval grade    [--traces F|DIR] [--dataset F] [--config F] [-o F] [--judge-provider P] [--judge-model M] [--judge-timeout S]
graph-agents-cli eval compare  BASELINE CANDIDATE [--fail-on-regression] [--json]
graph-agents-cli eval analyze  [--results F] [--output F] [--top-k K] [--judge] [--judge-provider P] [--judge-model M]
graph-agents-cli eval submit   [--results F] [--traces F] [--dataset F] [--dataset-name N] [--experiment N] [--endpoint URL]
graph-agents-cli eval metric list [--json]
```

- `eval generate` drives the local server (started like `run`, per runtime, stopped afterwards)
  or `--url` over the same `/chat` SSE API clients use, with the same credentials as `run`: a
  bearer credential goes in `GRAPH_AGENTS_CLI_API_KEY`, never on the command line (locally a
  `shared-bearer` project uses the `API_KEY` from `.env`; a `jwt` project needs a token, e.g.
  `export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice)"`);
  `--header` / `--cookie` are for a `custom` policy. `--dataset` defaults to `tests/eval/datasets/basic-dataset.json`, else
  every `*.json` there. Each case runs on a fresh `thread_id`; multi-message cases send messages
  in order on that thread and the trace records the final turn (plus every turn under `turns`).
  Exit 3 on a configuration error (no project, no dataset, malformed case, a local server port
  that is taken), 2 when a case is `error` or `missing` or the local server cannot start. The
  local server's port is the first free one of 18080-18089, or `GRAPH_AGENTS_CLI_RUN_PORT`; a
  SIGTERM or Ctrl-C stops it before the command exits.
- **`--url` runs the agent's tools for real in that environment.** Every case is a real chat as
  the identity the request authenticates as, so a tool that creates, updates, cancels or deletes
  data does it there (a dataset that places orders places real orders on every run). Before the
  first case, `eval generate`/`eval run` print a warning naming the target and the write methods
  the project's `api-policy.yaml` allows. Point `--url` only at an environment whose data you can
  reset, with a dedicated test identity; never at production data. All cases share one identity
  (`GRAPH_AGENTS_CLI_API_KEY`; `-H` only for a `custom` policy). Credentials in the URL are
  shown as `***@` and never written to traces or results; a 401 prints the policy's hint.
- `eval grade` runs the deterministic checks in the CLI process first; judge and custom metrics
  then run **inside the project's environment**: the CLI stages `.graph-agents-cli/judge_runner.py`
  into the project and runs it with `uv run python`, and the runner calls the template's
  `get_judge_model()` (so `JUDGE_*` from `.env` apply; `--judge-provider/--judge-model` override
  them for the run). Judges run only for the metrics a case declares and only for cases that
  passed the deterministic checks. No evaluation service; no LangChain in the CLI. `--traces`
  defaults to the newest traces file; a directory merges every `*.json` from one dataset.
- **What a judge sees.** On a multi-turn case, every earlier turn in full (the user message, each
  tool call with its result, the agent's reply), then the latest user message, the reply being
  scored and that reply's tool calls. A tool result longer than `judge.max_tool_result_chars`
  (default 50000 characters; `null` never cuts) is cut with a `[TRUNCATED ...]` marker saying how
  much the judge did not see, the groundedness rubric tells the judge not to count the omitted
  part as unsupported, and `eval grade` warns which cases were cut (`judge_notes` in the results).
- **The fake model is announced.** When the agent ran on `MODEL_PROVIDER=fake` on the local
  server, or the judge is the fake model, `eval grade` prints a warning above the result and
  appends "(fake model: plumbing check only, not a quality signal)" to "gate met"; the results
  record `fake_model` and `warnings`. For `--url` traces it warns when the project's own settings
  name the fake model (the target may run them). A case with `scope: all_turns` whose trace has
  no per-turn records is graded on its final turn, with a warning naming it.
- `eval run` validates the eval config and every case's metrics **before** generating (exit 3,
  no model calls spent; skipped when an `eval.grade` override is installed), then chains both on a
  fresh traces file and honours extension overrides of both `eval.generate` and `eval.grade`.
- `eval compare` takes two results files positionally; `eval analyze` reads the newest results
  file unless `--results` is given and clusters non-passed cases deterministically (the judge
  summarises clusters only with `--judge`).
- `eval submit` needs `LANGSMITH_API_KEY` and the `langsmith` extra; it is never required and is
  disabled in the disconnected profile. A LangSmith failure is one line (could not reach, refused
  the credentials, refused the upload) and exit 2.
- Artifact names are `<prefix>_<YYYYMMDD_HHMMSS>.json`; two runs within one second get a `_2`,
  `_3`, ... suffix.

## Local versus disconnected

Local orchestration removes the dependency on a hosted evaluation service and on LangSmith. It
does **not** make inference local: the agent model and the judge model run wherever
`MODEL_PROVIDER` and `JUDGE_*` point. Evaluation needs no network beyond those two endpoints, and
none at all when both are on-network OpenAI-compatible servers, which is the disconnected profile.
Say "runs locally" for orchestration on the developer's machine and "runs disconnected" only for
that profile.

---

## The eval loop

1. **Prepare data.** Edit `tests/eval/datasets/basic-dataset.json`. Start with 1-2 cases drawn
   from the spec's use cases. Datasets are versioned in the repo and reviewed in PRs.
2. **Run.** `graph-agents-cli eval run`. Paste the per-status counts and the exit code.
3. **Analyze.** Open the latest `results_<ts>.json`: each case has `status`, `reasons`, `checks`,
   `judge_scores` (an object per metric: `score`, `threshold`, `passed`, `quality`, `reasoning`,
   `kind` = `judge` or `custom`).
   For 10+ failures, `eval analyze` clusters them by status, check or metric, and masked reason
   (`--judge` adds root causes and fixes from the judge model).
4. **Fix.** Adjust the system prompt, tool descriptions, graph routing, or the case itself when it
   was wrong. Change one thing at a time.
5. **Repeat** until exit 0. Then add edge cases. Expect several iterations.

Use `eval compare before.json after.json` to prove a fix did not regress other cases.

### Choosing checks

| Need | Use |
|---|---|
| Response must mention / must not mention | `expect.contains`, `expect.not_contains` (case-insensitive; `case_insensitive: false` for exact case) |
| A multi-turn case: check every turn, not only the final reply | `expect.scope: all_turns` (replies, tool calls in order, each turn's latency, summed tokens) |
| Exact shape (id, number, format) | `expect.regex` |
| Structured output | `expect.json_schema` |
| The right tool with the right arguments | `expect.tool_calls: [{name, args_subset}]`, `ordered: true` when order matters |
| Must answer without tools | `expect.no_tool_calls: true` |
| Latency or token budget | `expect.max_latency_ms`, `expect.max_tokens` |
| Subjective quality, task completion, grounding | `judge.response_quality`, `judge.task_success`, `judge.groundedness` (needs `reference` or `context`) with a `threshold` |
| Allow a judge metric to pass below 100 % | list it under `quality_metrics:` with `threshold` and `min_pass_rate` |
| Policy regression (tool must not call a denied operation) | `expect.tool_calls` naming the allowed tool and `expect.not_contains` on the refusal text, or `expect.no_tool_calls` |

Prefer deterministic checks; they are the primary gate and cost nothing. Add a judge only for
what a substring cannot capture. Every judge metric is mandatory unless it is a designated
quality metric.

### `eval_config.yaml`

```yaml
judge: { provider: null, model: null }          # null = agent's provider/model (JUDGE_* env)
                                                 # max_tool_result_chars: 50000 (null = never cut)
quality_metrics:                                 # only these may be below 100 percent
  response_quality: { threshold: 4, min_pass_rate: 0.9 }
judges: {}                                       # {} = the three built-in rubrics (the scaffold default);
                                                 # override or add: <name>: { scale, rubric, prompt_template }
custom_metrics: []                               # python callables: module:function, run in the project env
```

`judge:` accepts only `provider`, `model` and `max_tool_result_chars`; any other key is exit 3.
A custom `prompt_template` may use exactly these placeholders: `{metric}`, `{rubric}`, `{scale}`,
`{conversation}`, `{transcript}`, `{response}`, `{reference}`, `{context}`,
`{reference_section}`, `{context_section}`, `{tool_calls_section}`; any other placeholder is a
configuration error (exit 3, when the config loads). `{conversation}` is every earlier turn in
full plus the latest user message, `{tool_calls_section}` the scored reply's tool calls, and
`{transcript}` the whole case including the scored reply. The scaffolded config is
`judge: {provider: null, model: null}`,
`quality_metrics: {response_quality: {threshold: 4, min_pass_rate: 0.9}}`, `judges: {}`,
`custom_metrics: []`, and the scaffolded `basic-dataset.json` (greeting, weather, capabilities,
and the two-turn weather-follow-up with `scope: all_turns`) passes on the `fake` provider with the
fake judge.

---

## Common gotchas

- **`missing` cases exit 2, not 1.** A case with no trace (server crashed, timeout, id typo) makes
  the run incomplete; fix generation before reading quality numbers.
- **A judge that cannot be reached is exit 3**, not a failed case. Check `JUDGE_*` and the key.
- **Quality metrics need both `threshold` and `min_pass_rate`**; a quality metric with no
  threshold is a configuration error (exit 3).
- **Tool-call assertions are on names and argument subsets**, not on wording; use
  `args_subset` for the fields you care about.
- **`contains` / `not_contains` ignore case** (`"hello"` matches "Hello!"; `not_contains:
  ["deleted"]` also catches "Deleted"). Set `expect.case_insensitive: false` for exact case, or
  use `regex`. A `not_contains` on a word the agent may legitimately say (a status name listed in
  a correct refusal) makes a flaky check; forbid the specific leak instead.
- **Multi-turn cases check the final turn by default.** `expect` reads the final reply and the
  final turn's tool calls; `scope: all_turns` reads every turn (a create-then-cancel case can then
  assert `create_order` then `cancel_order` with `ordered: true`). Judges always see every turn.
- **A quality rate counts only the cases scored on that metric.** Declaring `response_quality` on
  3 of 12 cases gives a rate over 3 cases, not 12.
- **Score fluctuates between runs:** the judge is a model. Lower `temperature` is already the
  default; write rubrics with concrete criteria; make the metric a quality metric with a
  `min_pass_rate` when variance is acceptable, never by loosening a mandatory check.
- **Tracing during eval:** traces are files under `artifacts/`; `TRACING_ENABLED` is separate and
  off by default. Eval never depends on run records.
- **A config or dataset in the previous template's format** (`metrics_to_run`, `eval_cases`) or a `prompt_template`
  with `{prompt}`/`{tool_calls}`: exit 3 when the config loads, before any case runs; use the
  placeholders above.
- **`MODEL_PROVIDER=fake` in `.env`** makes both the agent and the judge deterministic (score =
  scale maximum); useful to prove the harness, useless for behaviour. `eval grade` says so next to
  the result; never report such a "gate met" as a quality result.
- **A judge that mentions a "truncated" tool result**: the result was longer than
  `judge.max_tool_result_chars`; raise it (or set `null`) rather than loosening the metric.
- **Do not put behaviour checks in pytest.** They belong here.

---

## Proving your work

- After running eval, paste the per-status counts, the quality table, and the exit code, and say
  which provider the agent and the judge ran on (a run on the fake model proves the plumbing only).
- After a fix, show `eval compare` output for the case you fixed and confirm no regressions.
- Before deploy, re-run `eval run` and show every case; the exit code must be 0.

## CI

`pr_checks.yaml` runs `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli eval run` when
`tests/eval/datasets/*.json` exists and fails on non-zero, with extension overrides disabled
(`GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`), so a project extension cannot replace the gate. The unit
and integration tests always run on the fake model. The eval gate uses the project's real
provider and model (from the manifest) when that provider's key is a repository secret
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` or `MODEL_API_KEY`), or the
`MODEL_PROVIDER` / `MODEL_NAME` repository variables when set (`MODEL_NAME` is required when the
provider differs from the project's); `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL`
variables and a `JUDGE_API_KEY` secret configure a separate judge. Without a key the gate runs on
the deterministic `fake` provider (the scaffolded dataset passes that way) and prints the warning
"Eval gate is not a quality signal": it then only proves the plumbing (`eval grade` itself warns
the same way, locally too). A real provider sends eval prompts to it on every PR.

## Not covered by this skill

- Writing tools, graph nodes, or the auth policy: `/graph-agents-cli-langgraph-code`.
- Scaffold flags: `/graph-agents-cli-scaffold`.
- Deploying the agent the traces came from: `/graph-agents-cli-deploy`.
- Production tracing and run records: `/graph-agents-cli-observability`.
- Prompt optimization, user simulation, synthetic multi-turn datasets: not in this release.

## Migration note

Compared with google-agents-cli: grading no longer goes through the Agent Platform evaluation
service or Vertex AI; `eval optimize`, `eval dataset synthesize`, and `eval results` were removed;
`eval submit` uploads to LangSmith instead of creating a cloud eval run; the dataset schema is the
`cases` / `messages` / `expect` / `judge` shape in `references/dataset_schema.md`, not the
`eval_cases` / `Content` shape.
