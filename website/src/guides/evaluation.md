---
description: Evaluate a graph-agents-cli agent with datasets, deterministic checks and LLM judges, and enforce the gate in CI.
---

# Evaluation

<p class="gac-lede">Measure the agent with datasets, deterministic checks and LLM judges, and
enforce a gate whose exit code CI and coding agents act on. No evaluation service is involved:
the agent runs locally or where you point it, and grading runs on your machine.</p>

## How `eval run` works

```bash
graph-agents-cli eval run
```

`eval run` is `eval generate` followed by `eval grade`:

1. **Generate.** Every case in the dataset goes to the agent's `POST /chat` on a fresh thread;
   a multi-message case sends its user messages in order on that one thread. Without `--url`
   the project's local server is started (as `run` does) and stopped afterwards. The traces go
   to `artifacts/traces/traces_<timestamp>.json`.
2. **Grade.** Deterministic checks run in the CLI; judge and custom metrics run inside the
   project's environment (`uv run`), so the project's `JUDGE_*` settings apply. The results go to
   `artifacts/grade_results/results_<timestamp>.json`.
3. **Gate.** The exit code says whether the gate is met. `eval run` returns the worse of its two
   stages.

The dataset defaults to `tests/eval/datasets/basic-dataset.json`, else every `*.json` in
`tests/eval/datasets/`; `--dataset` names a file or directory. `eval run` validates the config
and every case's metrics before generating, so a configuration error costs no model calls.

## The gate

A run meets the gate when:

- **every planned case is accounted for**: no case is `error` or `missing`;
- **every deterministic `expect` check passes**;
- **every judge metric a case declares passes its threshold**, unless it is a quality metric;
- **each metric under `quality_metrics`** in `tests/eval/eval_config.yaml` reaches its
  `min_pass_rate`, a rate over the cases scored on that metric (a case that does not declare it
  is not counted as a pass).

Mandatory checks and case accounting cannot be relaxed by configuration: there is no run-wide
pass rate. Every planned case ends in one status:

| Status | Meaning |
|---|---|
| `passed` | every mandatory item passed, and every quality metric met its threshold |
| `failed` | a deterministic check or a mandatory judge metric failed |
| `quality_below_threshold` | mandatory items passed, a quality metric scored under its threshold |
| `error` | generating or grading the case raised (a server error, an unmatched approval gate, a judge exception) |
| `missing` | no trace, or a trace without a response |

| Exit code | Meaning |
|---|---|
| 0 | gate met |
| 1 | a case `failed`, or a quality metric is under its `min_pass_rate` |
| 2 | a case is `error` or `missing` (an incomplete run: no quality rate is computed), or the agent could not be reached |
| 3 | configuration error: no project, no dataset, a malformed case, an unknown metric, an unreachable judge, a quality metric with no threshold |

The exit code is the gate: never read the scores and declare success on a non-zero exit.
[Exit codes](../reference/exit-codes.md) has the scheme every command follows.

## Write a case

A dataset is a JSON file with a list of `cases`. From the generated
`tests/eval/datasets/basic-dataset.json`:

```json title="tests/eval/datasets/basic-dataset.json"
{
  "cases": [
    {
      "id": "weather",
      "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
      "expect": {
        "contains": ["sunny"],
        "tool_calls": [{"name": "get_weather", "args_subset": {"query": "Paris"}}]
      },
      "reference": "It's 90 degrees and sunny in Paris.",
      "context": "get_weather returns \"It's 90 degrees and sunny.\" for every place except San Francisco.",
      "metadata": {"category": "tools"}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | unique in the dataset; the gate accounts for every id |
| `messages` | the user turns, sent in order on one thread; the final assistant reply is graded |
| `expect` | deterministic checks; every one given is mandatory |
| `judge` | judge metrics and thresholds: `{"task_success": {"threshold": 4}}` |
| `reference` | the expected answer, for `task_success` |
| `context` | grounding text, for `groundedness` |
| `approvals` | how a person would decide each gated call the case reaches ([below](#cases-that-reach-an-approval-gate)) |
| `metadata` | free-form, carried into traces and results |

The eval skill's [dataset schema](https://github.com/ss7172/graph-agents-cli/blob/main/skills/graph-agents-cli-eval/references/dataset_schema.md)
has every field, the trace and results formats, and more examples. Write cases for your tools,
their refusals, failure modes and instructions planted in tool data; replace the weather cases
when you replace the weather tool.

## Deterministic checks

`graph-agents-cli eval metric list` prints the catalogue, with the project's own judges and
metrics:

| Check (`expect.<name>`) | Passes when |
|---|---|
| `contains` / `not_contains` | every listed substring appears / none appears in the response (case-insensitive) |
| `regex` | the response matches (`re.search`, DOTALL; `(?i)` ignores case) |
| `json_schema` | the final answer validates against the schema: a project with a [response schema](develop.md#structured-final-answers) has it checked as the object the run returned (`structured_response`); otherwise the final reply's JSON (the whole reply, else its last JSON object or array of the schema's root type) |
| `tool_calls` | the listed tools were called, matched by name and `args_subset` (`ordered: true` for order) |
| `no_tool_calls` | the agent made no tool call |
| `max_latency_ms`, `max_tokens` | `latency_ms`, and input plus output tokens, stay at or below the limit |
| `approvals` | the listed calls hit an approval gate, each with its status (`gated`, `approved`, `rejected`) |
| `no_approvals` | no call hit an approval gate |

Two modifiers change how checks read the response:

- **`case_insensitive`** (default `true`): `contains` and `not_contains` ignore case, so
  `not_contains: ["deleted"]` also fails on "Deleted". Set `false` for exact case.
- **`scope`** (default `final_turn`): a multi-turn case's checks read the final turn.
  `all_turns` reads every turn's replies, tool calls, latency, tokens and gates (`json_schema`
  always reads the final answer). A trace without per-turn records is graded on its final turn,
  and `eval grade` says which cases.

## Judges and quality metrics

`tests/eval/eval_config.yaml` configures the judge and names the quality metrics:

```yaml title="tests/eval/eval_config.yaml"
judge:
  provider: null   # null = JUDGE_MODEL_PROVIDER, else the agent's MODEL_PROVIDER
  model: null      # null = JUDGE_MODEL_NAME, else MODEL_NAME
  # max_tool_result_chars: 50000

quality_metrics:
  response_quality: { threshold: 4, min_pass_rate: 0.9 }

judges: {}           # override the built-in rubrics, or add your own
custom_metrics: []   # module:function callables run in the project's environment
```

| Built-in judge | Scores | Needs |
|---|---|---|
| `response_quality` | accuracy, relevance and clarity of the final response | nothing extra |
| `task_success` | whether the agent did what the user asked, counting earlier turns | `reference` recommended |
| `groundedness` | whether every claim is supported by `context` or tool results | `context` or tool results |

- **Mandatory or quality.** A judge metric a case declares is mandatory: a score under its
  threshold fails the case. Listed under `quality_metrics`, it may miss on some cases, as long
  as the pass rate over the cases scored on it reaches `min_pass_rate`.
- **Judges see every turn**: each earlier user message, tool call with its result, and agent
  reply, then the reply being scored. A tool result longer than `judge.max_tool_result_chars`
  (default 50000 characters; `null` never cuts) is cut with a marker telling the judge how much
  it did not see, and `eval grade` warns which cases were cut.
- **Custom rubrics** go under `judges:` (`scale`, `rubric`, `prompt_template`). A template may
  use `{transcript}` for the whole case, among `{conversation}`, `{response}`, `{reference}`,
  `{context}` and the other placeholders the eval skill lists; an unknown placeholder is exit 3.
- **The judge model** resolves in this order: `--judge-provider` / `--judge-model`, then
  `judge:` in the config, then `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL` and
  `JUDGE_API_KEY`, then the agent's model. Prefer a judge at least as capable as the agent.

## Run it

With the scaffolded dataset on the fake model:

```text title="Output"
─────────────────────────── Step 1/2: eval generate ────────────────────────────
Running 4 case(s) from tests/eval/datasets/basic-dataset.json
 greeting: ok (8 ms)
 capabilities: ok (13 ms)
 weather: ok (11 ms)
 weather-follow-up: ok (4 ms)
───────────────────────────── Step 2/2: eval grade ─────────────────────────────
Evaluation gate
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Status                  ┃ Cases ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ passed                  │     4 │
│ failed                  │     0 │
│ quality_below_threshold │     0 │
│ error                   │     0 │
│ missing                 │     0 │
│ planned                 │     4 │
└─────────────────────────┴───────┘
Quality metrics
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━┳━━━━━┓
┃ Metric           ┃ Pass rate ┃ Passed/scored ┃ Min ┃ Met ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━╇━━━━━┩
│ response_quality │      100% │           2/2 │ 90% │ yes │
└──────────────────┴───────────┴───────────────┴─────┴─────┘
Warning: the agent ran on the deterministic fake model (MODEL_PROVIDER=fake): its replies are canned, so this run proves the eval plumbing only, not the agent's behaviour. Run it on the project's real provider before trusting the gate.
Warning: the judge is the deterministic fake model (provider 'fake'): it gives every judge metric the maximum score without reading the reply, so the judge scores and quality rates are not a quality signal. Set JUDGE_MODEL_PROVIDER (or judge.provider in eval_config.yaml) to a real provider to measure quality.
Result: gate met (exit code 0) (fake model: plumbing check only, not a quality signal)
```

A failed case is listed with its reasons, and the result is `gate failed (exit code 1)`:

```text title="Output"
Cases needing attention
┏━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Case    ┃ Status ┃ Reasons                                     ┃
┡━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ weather │ failed │ contains: response does not contain 'rainy' │
└─────────┴────────┴─────────────────────────────────────────────┘
```

!!! warning "The fake model proves the plumbing only"

    With `MODEL_PROVIDER=fake`, for the agent or the judge, the replies and scores are canned:
    "gate met" says the harness works, not that the agent does. Run the gate on a real
    provider before trusting it, and expect several rounds of fixing prompts, tools and cases
    before it passes.

Credentials are those of `run`: a `shared-bearer` project uses the `API_KEY` in `.env`; a `jwt`
project needs a token in `GRAPH_AGENTS_CLI_API_KEY`, for example from `auth dev-token`
([Authentication](authentication.md#clients-and-credentials)).

## Against a deployed agent

`eval run --url` (and `eval generate --url`) drive a running agent instead of the local server:

```bash
export GRAPH_AGENTS_CLI_API_KEY=...   # the test identity's credential, not a --header
graph-agents-cli eval run --url https://agent-staging.example.com
```

!!! danger "Its tools run for real there"

    Every case is a real chat as the identity the requests authenticate as: a case that
    creates, updates or cancels data does so in that environment, and an approved gated call is
    sent there. The command prints a warning naming the target and the write methods the
    project's `api-policy.yaml` allows before the first case. Use an environment whose data you
    can reset and a dedicated test identity, never production data.

- Credentials in the URL itself are shown as `***@` and never stored, but they replace the
  bearer token: leave them out
  ([KI-068](../reference/known-issues.md#ki-068-credentials-in-the-url-of-eval-replace-the-bearer-token)).
- The agent there does not report its model, so traces and results record `model: null`. When
  the project's own settings name the fake model, `eval grade` warns that the target may be
  running them.

## Cases that reach an approval gate

A case that reaches a call the API policy [gates](approvals.md) says how a person would decide
it; `eval generate` decides each gate by the first matching instruction and the run continues:

```json
{
  "id": "cancel-own-order",
  "messages": [{"role": "user", "content": "Cancel the order for ORD-1001"}],
  "approvals": [
    {"decision": "approve", "match": {"operation_id": "cancelOrder"}}
  ],
  "expect": {
    "tool_calls": [{"name": "cancel_order", "args_subset": {"order_id": "ORD-1001"}}],
    "approvals": [
      {"match": {"method": "POST", "path": "/orders/{order_id}/cancel"}, "status": "approved"}
    ]
  }
}
```

- **`match`** names the call by `operation_id`, or by `method` and `path` (a template matches
  any record; a concrete path only that record), optionally narrowed by `api`.
- **`expect.approvals`** checks each gate's outcome: `gated` (reached the gate, however it
  ended), `approved` or `rejected`. **`expect.no_approvals: true`** asserts that no call reached
  a gate: in an injection case, the planted write never even got as far as asking.
- **A gate no instruction matches makes the case `error`.** The eval never approves on its own:
  it rejects that gate (and one whose decision the server refused), so no approval is left
  pending. A gate it may not reject goes with the case's thread, which the eval identity
  deletes. The trace records how in `approvals[].cleanup`: `rejected`, `not_pending`,
  `thread_deleted`, or `left_pending` when the thread could not be deleted either (the case
  error names it: it waits until it expires or an approver decides it).

```text title="Output"
 no-instruction: error (unexpected approval gate on POST /orders/ORD-1003/cancel (api orders, operation_id cancelOrder): the case has no approvals instruction matching it; add one, e.g. {"decision": "reject", "match": {...}}, saying what a human would decide)
```

A gate that lists `requester` is decided as the eval identity (it started the run). Any other is
decided with `GRAPH_AGENTS_CLI_APPROVER_API_KEY` when it is set: the credential of a principal
holding the gate's role. One dataset can mix both.

## Compare, analyze, submit

| Command | What it does |
|---|---|
| `eval compare BASELINE CANDIDATE` | Per-case status changes, quality pass-rate deltas and summary deltas; `--fail-on-regression` exits 1 on a regression, `--json` prints the comparison |
| `eval analyze` | Clusters the failed, quality-below-threshold, error and missing cases of the newest results by reason, deterministically; `--judge` asks the judge for root causes per cluster |
| `eval submit` | Uploads the dataset and a results file to LangSmith as an experiment: needs `LANGSMITH_API_KEY` and the CLI's `langsmith` extra |
| `eval metric list` | The checks, modifiers and judges available, with the project's own |

```bash
graph-agents-cli eval compare artifacts/grade_results/results_A.json \
  artifacts/grade_results/results_B.json --fail-on-regression
```

```text title="Output"
Case status changes
┏━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Case    ┃ Baseline ┃ Candidate ┃ Change    ┃
┡━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ weather │ passed   │ failed    │ regressed │
└─────────┴──────────┴───────────┴───────────┘
Regressions:
  - weather: passed -> failed
  - exit code 0 -> 1
```

`eval submit` needs the extra in the CLI's own environment:
`uv tool install --force 'graph-agents-cli[langsmith] @ git+https://github.com/ss7172/graph-agents-cli@v0.3.0'`.

## In CI

The generated `pr_checks` workflow runs `graph-agents-cli eval run` on every pull request when a
dataset exists, with extension overrides disabled (`GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`), so a
project extension cannot replace the gate. The unit and integration tests always run on the
fake model; the eval gate uses a real model when:

- the provider's key is a repository secret (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GOOGLE_API_KEY` or `MODEL_API_KEY`), or
- the `MODEL_PROVIDER` repository variable is set (with `MODEL_NAME` when it names another
  provider than the project's). `MODEL_NAME` alone only changes the model once a key or
  `MODEL_PROVIDER` has switched the gate to a real one.

`JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME` and `JUDGE_BASE_URL` variables and a `JUDGE_API_KEY`
secret pick a separate judge. Without a key the gate runs on the fake model and warns that it
is not a quality signal. A real provider receives the eval prompts on every pull request.
[CI/CD](cicd.md) covers the workflows and the GitHub settings they need.

## Known limitations

!!! info "Evaluation"

    - Every case runs as one identity (plus the approver credential for role gates): split
      role-specific cases into datasets run with different credentials
      ([KI-065](../reference/known-issues.md#ki-065-every-eval-case-runs-as-one-identity)).
    - No overall size limit for a judge prompt: a long multi-turn case can exceed the judge's
      context window
      ([KI-067](../reference/known-issues.md#ki-067-no-overall-size-limit-for-a-judge-prompt)).
    - When a case approves a gated call, the recorded response joins the text before and after
      the pause with no separator: check words only the final reply uses, or `expect.approvals`
      ([KI-069](../reference/known-issues.md#ki-069-eval-joins-the-text-before-and-after-an-approval-with-no-separator)).
    - A run that pauses on more than 20 gated calls can leave an approval pending: check
      `approvals list` after a `--url` run
      ([KI-027](../reference/known-issues.md#ki-027-eval-generate-can-leave-an-approval-pending-after-more-than-20-gated-calls)).
    - `eval run` prints no setup hint for a 503 from the local server: run
      `graph-agents-cli login` or `run "hi"` to see it
      ([KI-070](../reference/known-issues.md#ki-070-eval-run-prints-no-setup-hint-for-a-503-from-the-local-server)).
    - No prompt optimisation, dataset synthesis, user simulation or results fetch: cases are
      written by hand
      ([KI-071](../reference/known-issues.md#ki-071-evaluation-is-narrower-than-upstreams)).

## Next steps

<div class="grid cards" markdown>

-   :material-account-check-outline:{ .lg } **[Human approval](approvals.md)**

    Gate the writes your eval cases exercise.

-   :material-source-pull:{ .lg } **[CI/CD](cicd.md)**

    The workflows that run the gate on every pull request.

-   :material-console:{ .lg } **[`eval run`](../reference/cli.md#graph-agents-cli-eval-run)**

    Every flag of `eval run` and the other `eval` commands.

-   :material-robot-outline:{ .lg } **[The eval skill](../reference/skills.md#graph-agents-cli-eval)**

    What your coding agent knows about the eval loop.

</div>
