# Evaluation dataset, trace, and results schemas

Paths: datasets `tests/eval/datasets/*.json`, config `tests/eval/eval_config.yaml`, traces
`artifacts/traces/traces_<YYYYMMDD_HHMMSS>.json`, results
`artifacts/grade_results/results_<ts>.json`, analyses `artifacts/analysis_<ts>.json`. Timestamps
are local time; when a name already exists (two runs within one second) a `_2`, `_3`, ... suffix
is added before the extension. `eval grade` picks the newest traces file by mtime.

## Dataset

```json
{
  "cases": [
    {
      "id": "greeting",
      "messages": [{"role": "user", "content": "hi"}],
      "expect": {
        "contains": ["hello"],
        "not_contains": [],
        "regex": null,
        "json_schema": null,
        "tool_calls": [{"name": "get_weather", "args_subset": {"query": "SF"}}],
        "ordered": false,
        "no_tool_calls": false,
        "max_latency_ms": null,
        "max_tokens": null
      },
      "judge": { "response_quality": { "threshold": 4 } },
      "reference": "optional reference answer",
      "context": "optional grounding context",
      "metadata": {}
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | unique within the dataset; used for planned-case accounting |
| `messages` | yes | ordered user turns (`role: user`); each is sent as a `/chat` message on the case's thread; the response graded is the final assistant reply |
| `expect` | no | deterministic checks; every key optional; all present checks are mandatory |
| `expect.contains` / `not_contains` | | case-insensitive substrings of the final response |
| `expect.regex` | | Python regex searched in the final response |
| `expect.json_schema` | | the response must parse as JSON and validate |
| `expect.tool_calls` | | list of `{name, args_subset}`; each must appear in the trace's `tool_calls` with the subset of args matching; `ordered: true` requires the same relative order |
| `expect.no_tool_calls` | | the trace must contain no tool call |
| `expect.max_latency_ms`, `max_tokens` | | upper bounds on `latency_ms` and `usage.input_tokens + output_tokens` |
| `judge` | no | map of metric name to `{threshold}`; metrics must be a built-in (`response_quality`, `task_success`, `groundedness`), a `judges:` entry, or a `custom_metrics:` callable in `eval_config.yaml`. The threshold resolves as case `judge.<m>.threshold` > `quality_metrics.<m>.threshold` > `judges.<m>.threshold` > custom-metric default (1.0); none found is exit 3 |
| `reference` | for `task_success`, optional otherwise | the expected answer the judge compares against |
| `context` | for `groundedness` | the grounding text the response must be supported by |
| `metadata` | no | free-form; carried into traces and results (tags, owner, story id) |

Common mistakes:

- Missing `id` or duplicate ids: configuration error (exit 3).
- Putting the assistant's expected wording in `messages`; only user turns go there, expectations
  go in `expect` or `reference`.
- `tool_calls` with the full argument set; use the subset that matters.
- A `judge` metric that is not declared in `eval_config.yaml`: exit 3.
- Expecting a `quality_metrics` entry to relax `expect` checks: it never does.

## Trace file (written by `eval generate`)

```json
{
  "dataset_hash": "sha256...",
  "generated_at": "2026-09-22T10:00:00+00:00",
  "agent_version": "0.1.0",
  "model": "openai:gpt-5-mini",
  "traces": [
    {
      "case_id": "greeting",
      "status": "ok",
      "response": "Hello! ...",
      "tool_calls": [{"name": "get_weather", "args": {"query": "SF"}, "result": "60F", "is_error": false}],
      "usage": {"input_tokens": 120, "output_tokens": 40},
      "latency_ms": 850,
      "error": null,
      "thread_id": "…",
      "run_id": "…",
      "agent_version": "0.1.0",
      "model": "openai/gpt-5-mini",
      "case": { "...the dataset case as written..." }
    }
  ]
}
```

`status` is `ok`, `error` (generation raised, the stream ended with an `error` event, or
`message.end` carried a status other than `ok`, such as `step_limit` when the run reached
`RECURSION_LIMIT`), or `missing` (no events at all). A completed run with an empty reply is `ok` with `response: ""` and
is graded. Values are derived from the SSE events `message.delta`, `tool.call`, `tool.result`,
`message.end`, `error`; `model` is `<provider>/<model>` as the app labels it.

Additive keys the implementation writes (all contract keys above are present unchanged): the
wrapper also carries `dataset_paths` (project-relative dataset files), `base_url` and
`app_name`; each trace carries `case` (the original dataset case, so `eval grade` can work from
the trace file alone) and, only for cases with several user messages, `turns` (one record per
turn; the top-level `response`/`tool_calls`/`usage`/`latency_ms` are the final turn's).
`eval grade --dataset` re-reads the dataset instead of `case`.

## Results file (written by `eval grade`)

```json
{
  "dataset_hash": "sha256...",
  "graded_at": "2026-09-22T10:05:00+00:00",
  "judge": {"provider": "openai", "model": "gpt-5-mini"},
  "capture": "metadata",
  "summary": {"passed": 8, "failed": 1, "quality_below_threshold": 1, "error": 0, "missing": 0, "exit_code": 1},
  "quality": {"response_quality": {"pass_rate": 0.9, "min_pass_rate": 0.9, "met": true}},
  "cases": [
    {
      "id": "greeting",
      "status": "failed",
      "reasons": ["contains: response does not contain hello"],
      "checks": {"contains": false, "tool_calls": true},
      "judge_scores": {
        "response_quality": {"score": 5, "threshold": 4, "passed": true, "quality": true, "reasoning": "...", "error": null, "kind": "judge"}
      }
    }
  ]
}
```

`summary.exit_code` is what the command returned. `quality.<metric>.pass_rate` is the fraction of
planned cases not `quality_below_threshold` on that metric; it is only computed when no case is
`error` or `missing` (`pass_rate` and `met` are `null` on an incomplete run).

Additive keys the implementation writes: top-level `generated_at`, `agent_version`, `model`,
`traces_files`, `dataset_paths`, `traces_dataset_hash` (the dataset the traces came from; differs
from `dataset_hash` only when `--dataset` graded stale traces, which `eval compare` warns about),
`config` (the effective eval config) and `planned` (the planned
case ids); `quality.<metric>.below_threshold` (count); and `cases[*].judge_scores.<metric>` is the
object shown above (`score`, `threshold`, `passed`, `quality` = whether the metric is a quality
metric, `reasoning`, `error`, `kind` = `judge` for a model judge or `custom` for a
`custom_metrics` callable, whose errors read `custom metric <name>: ...` and which `eval analyze`
groups as `error/custom`) rather than a bare number. Cases already `failed`, `error` or
`missing` have empty `judge_scores` (judges are not called for them).

## `eval_config.yaml`

```yaml
judge: { provider: null, model: null }          # null = agent's provider/model
quality_metrics:                                 # only these may be below 100 percent
  response_quality: { threshold: 4, min_pass_rate: 0.9 }
judges:                                          # rubric text is versioned here
  response_quality: { scale: 5, rubric: "...", prompt_template: "..." }
  task_success: { scale: 5, rubric: "...", prompt_template: "..." }
  groundedness: { scale: 5, rubric: "...", prompt_template: "..." }
custom_metrics: []                               # python callables: module:function
```

- `judge.provider` / `judge.model` override `JUDGE_*` from the environment for this project
  (`null` = `JUDGE_MODEL_PROVIDER`/`JUDGE_MODEL_NAME`, else the agent's `MODEL_*`).
- `judges: {}` is valid and means the three built-in rubrics; an entry overrides or adds one
  (`scale`, `rubric`, `prompt_template`). A custom `prompt_template` may use exactly `{metric}`,
  `{rubric}`, `{scale}`, `{conversation}`, `{response}`, `{reference}`, `{context}`,
  `{reference_section}`, `{context_section}`, `{tool_calls_section}`; anything else is exit 3.
- `quality_metrics.<name>` needs both `threshold` (per-case score) and `min_pass_rate`
  (aggregate fraction of planned cases, default `1.0`).
- A case's `judge.<name>.threshold` overrides the config threshold for that case.
- `custom_metrics` entries are `module:function` callables
  `fn(case: dict, trace: dict) -> bool | number | {"score": n, "reasoning": str}` (default
  threshold `1.0`), run inside the project's environment through the staged judge runner, applied
  to every case, and treated like judge metrics (mandatory unless designated quality).
