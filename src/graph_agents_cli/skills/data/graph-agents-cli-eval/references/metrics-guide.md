# Metrics guide

Two kinds of checks: **deterministic `expect` checks** (in-process, no model, always mandatory)
and **judge metrics** (a chat model scores the response against a rubric; mandatory unless
designated a quality metric). `graph-agents-cli eval metric list` prints the live set.

## Deterministic checks (`expect`)

| Check | Passes when | Typical use |
|---|---|---|
| `contains: [..]` | every substring occurs in the final response (case-insensitive) | key facts, required disclaimers |
| `not_contains: [..]` | none occurs | leaked secrets, forbidden claims, refusal text when a call should have succeeded |
| `regex: "..."` | `re.search` matches | ids, dates, numeric formats |
| `json_schema: {..}` | response parses as JSON and validates against the schema | structured output modes |
| `tool_calls: [{name, args_subset}]` | each entry matches a trace tool call by name with `args_subset` a subset of the recorded args; `ordered: true` enforces relative order | trajectory assertions, policy regressions (`getIncident` called, nothing else) |
| `no_tool_calls: true` | the trace has no tool call | answers that must come from the prompt alone |
| `max_latency_ms: n` | `latency_ms <= n` | latency budget |
| `max_tokens: n` | `input_tokens + output_tokens <= n` | cost budget |

All present checks must pass; a failed check marks the case `failed` with a reason.

## Built-in judges

Built into the CLI; `judges: {}` in `eval_config.yaml` (the scaffold default) uses them as they
are, and a `judges.<name>` entry overrides `scale`, `rubric` or `prompt_template` (versioned in the
repo, so a rubric change is a reviewed diff). A custom `prompt_template` may use exactly
`{metric}`, `{rubric}`, `{scale}`, `{conversation}`, `{response}`, `{reference}`, `{context}`,
`{reference_section}`, `{context_section}`, `{tool_calls_section}`; any other placeholder is a
configuration error (exit 3). The default template asks for a JSON verdict with a `score`, which
is why the template's `fake` judge answers `{"score": 5, ...}`.

| Metric | Scores | Needs |
|---|---|---|
| `response_quality` | overall helpfulness, correctness, tone against the rubric | nothing extra |
| `task_success` | whether the response accomplishes the case's task; compares against `reference` when present | `reference` recommended |
| `groundedness` | whether every claim is supported by `context` (or tool results when no context) | `context` |

Add a judge to a case with `"judge": {"task_success": {"threshold": 4}}`. Scores are integers on
the configured scale; `>= threshold` passes.

## Mandatory versus quality metrics

- A judge metric a case declares is **mandatory**: a score below threshold marks the case
  `failed`.
- A judge metric listed under `quality_metrics:` is a **quality metric**: a score below its
  `threshold` marks the case `quality_below_threshold` (not `failed`), and the run passes if the
  fraction of planned cases not below threshold is at least `min_pass_rate`.
- A judge call that raises marks the case `error` (exit 2); an unreachable judge, an unknown
  metric or a metric with no threshold is a configuration error (exit 3). Judges are not called
  for cases that already `failed` (deterministic checks come first).
- Deterministic checks can never be quality metrics.
- Judges run inside the project's environment: `eval grade` stages
  `.graph-agents-cli/judge_runner.py` into the project and runs it with `uv run python`; the
  runner calls `app.app_utils.model.get_judge_model()` with no arguments. Provider `fake` scores
  every metric at the scale maximum.

```yaml
quality_metrics:
  response_quality: { threshold: 4, min_pass_rate: 0.9 }   # 90 % of cases may score >= 4
```

## Custom metrics

```yaml
custom_metrics:
  - tests.eval.metrics:answer_length_ok
```

```python
# tests/eval/metrics.py
def answer_length_ok(case: dict, trace: dict) -> dict:
    words = len((trace.get("response") or "").split())
    return {
        "score": 1 if words <= case.get("metadata", {}).get("max_words", 200) else 0,
        "reasoning": f"{words} words",
    }
```

Signature `fn(case: dict, trace: dict) -> bool | number | {"score": n, "reasoning": str}`;
default threshold `1.0`. Custom metrics are config-level: they run for **every** case through
the judge runner inside the project's environment (so they may import project modules and call
`from app.app_utils.model import get_judge_model` for bespoke rubrics); a case's
`judge.<name>.threshold` only overrides the threshold. They follow the same mandatory/quality
rule as judge metrics (list one under `quality_metrics:` to allow a pass rate below 100 %).

## Judge configuration

Resolution order: `eval grade --judge-provider` / `--judge-model` -> `eval_config.yaml`
`judge: {provider, model}` -> `JUDGE_MODEL_PROVIDER` / `JUDGE_MODEL_NAME` / `JUDGE_BASE_URL` /
`JUDGE_API_KEY` -> the agent's `MODEL_*`. The CLI passes its choice to the runner as
`JUDGE_MODEL_PROVIDER` / `JUDGE_MODEL_NAME`, and the template's `get_judge_model()` reads them at
call time and builds the judge with `init_chat_model` like the agent. In the disconnected profile it is an on-network
OpenAI-compatible server. Prefer a judge at least as capable as the agent model; the same model
judging itself is acceptable for structural rubrics, weaker for quality.

## What to fix when a metric fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `contains` fails but the answer is right | wording variance | use `regex` or a judge; or make the prompt ask for the phrase explicitly |
| `tool_calls` fails: wrong tool | tool descriptions overlap | sharpen docstrings; remove unused tools |
| `tool_calls` fails: no call | model does not call tools | on `openai-compatible`, check the model supports tools and the server parses them |
| `tool_calls` fails: refused by policy | the call is outside `api-policy.yaml` | the operation must be allowed in `api-policy.yaml` by its owner (and declared in `API_CALLS`), or the tool must change |
| `groundedness` low | the answer adds unsupported claims | instruct the model to cite tool results; return structured tool output |
| `task_success` low with a `reference` | the reference is too specific | rewrite the reference as the essential content, not exact wording |
| `max_latency_ms` fails | tool or model slow | measure with `run -v`; cache; reduce context |
| `error` status | server crash, timeout, judge exception | read `reasons`; fix generation before grading |
