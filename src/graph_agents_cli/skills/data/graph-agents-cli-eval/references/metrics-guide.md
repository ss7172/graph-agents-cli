# Metrics guide

Two kinds of checks: **deterministic `expect` checks** (in-process, no model, always mandatory)
and **judge metrics** (a chat model scores the response against a rubric; mandatory unless
designated a quality metric). `graph-agents-cli eval metric list` prints the live set.

## Deterministic checks (`expect`)

| Check | Passes when | Typical use |
|---|---|---|
| `contains: [..]` | every substring occurs in the final response (case-insensitive) | key facts, required disclaimers |
| `not_contains: [..]` | none occurs (case-insensitive) | leaked secrets, forbidden claims, refusal text when a call should have succeeded |
| `regex: "..."` | `re.search` matches (`(?i)` to ignore case) | ids, dates, numeric formats |
| `json_schema: {..}` | the final response parses as JSON and validates against the schema | structured output modes |
| `tool_calls: [{name, args_subset}]` | each entry matches a trace tool call by name with `args_subset` a subset of the recorded args; `ordered: true` enforces relative order | trajectory assertions, policy regressions (`getIncident` called, nothing else) |
| `no_tool_calls: true` | the trace has no tool call | answers that must come from the prompt alone |
| `max_latency_ms: n` | `latency_ms <= n` | latency budget |
| `max_tokens: n` | `input_tokens + output_tokens <= n` | cost budget |
| `approvals: [{match, status}]` | each entry matches a distinct gate the run hit (trace `approvals`): `match` by `operation_id` and/or `method` + `path` (template), optional `api`; `status` `gated` (default, any outcome), `approved` or `rejected` | writes that must wait for a human, and how the case decided them |
| `no_approvals: true` | no call reached an approval gate | injection cases: the planted write never got as far as asking |

All present checks must pass; a failed check marks the case `failed` with a reason.

Modifiers (keys of `expect` that are not checks; `eval metric list` shows them too):

| Modifier | Default | Effect |
|---|---|---|
| `ordered` | `false` | `tool_calls` must appear in the listed order |
| `case_insensitive` | `true` | `contains` / `not_contains` ignore case (casefold); `false` for exact case |
| `scope` | `final_turn` | on a multi-turn case, `final_turn` reads the final reply and its tool calls, latency and usage; `all_turns` reads every turn: `contains`/`regex` pass when any reply matches, `not_contains` fails when any does, `tool_calls`/`no_tool_calls` read every call in order, `approvals`/`no_approvals` every gate, `max_latency_ms` bounds each turn, `max_tokens` their sum. `json_schema` always reads the final reply |

Case-insensitive matching is the safer default for `not_contains`: a refusal check such as
`not_contains: ["deleted"]` must also fail on "Deleted ORD-1008.".

## Built-in judges

Built into the CLI; `judges: {}` in `eval_config.yaml` (the scaffold default) uses them as they
are, and a `judges.<name>` entry overrides `scale`, `rubric` or `prompt_template` (versioned in the
repo, so a rubric change is a reviewed diff). A custom `prompt_template` may use exactly
`{metric}`, `{rubric}`, `{scale}`, `{conversation}`, `{transcript}`, `{response}`,
`{reference}`, `{context}`, `{reference_section}`, `{context_section}`, `{tool_calls_section}`;
any other placeholder is a configuration error (exit 3, when the config loads). The default
template asks for a JSON verdict with a `score`, which is why the template's `fake` judge answers
`{"score": 5, ...}`.

What every judge prompt contains, so a follow-up is judged in context:

- `{conversation}`: each earlier turn in full (the user message, every tool call the agent made
  with its result, the agent's reply), then the latest user message. Dataset `system`/`assistant`
  messages appear in place, marked as not sent to the agent.
- `{response}` and `{tool_calls_section}`: the reply being scored and its own tool calls.
- `{transcript}`: all of the above as one block, for custom templates.
- A tool result longer than `judge.max_tool_result_chars` (default 50000 characters; `null`
  never cuts) ends in `[TRUNCATED by graph-agents-cli: the judge sees the first N of M characters
  ...]`, and the groundedness rubric tells the judge not to treat a claim from the omitted part as
  unsupported. `eval grade` warns which cases were cut; raise the limit rather than the threshold.

| Metric | Scores | Needs |
|---|---|---|
| `response_quality` | overall helpfulness, correctness, tone against the rubric | nothing extra |
| `task_success` | whether the response accomplishes the case's task, counting what earlier turns already did; compares against `reference` when present | `reference` recommended |
| `groundedness` | whether every claim is supported by `context` or by the tool results of any turn | `context` or tool results |

Add a judge to a case with `"judge": {"task_success": {"threshold": 4}}`. Scores are integers on
the configured scale; `>= threshold` passes.

## Mandatory versus quality metrics

- A judge metric a case declares is **mandatory**: a score below threshold marks the case
  `failed`.
- A judge metric listed under `quality_metrics:` is a **quality metric**: a score below its
  `threshold` marks the case `quality_below_threshold` (not `failed`), and the run passes if, of
  the cases scored on that metric, the fraction at or above the threshold is at least
  `min_pass_rate`. Cases that do not declare the metric are not in the denominator (the results
  record `scored` and `passed` per metric, and the table prints them); a quality metric no case
  ran is `n/a (no case ran it)`.
- A judge call that raises marks the case `error` (exit 2); an unreachable judge, an unknown
  metric or a metric with no threshold is a configuration error (exit 3). Judges are not called
  for cases that already `failed` (deterministic checks come first).
- Deterministic checks can never be quality metrics.
- Judges run inside the project's environment: `eval grade` stages
  `.graph-agents-cli/judge_runner.py` into the project and runs it with `uv run python`; the
  runner calls `app.app_utils.model.get_judge_model()` with no arguments. Provider `fake` scores
  every metric at the scale maximum, and `eval grade` then warns that the result is not a quality
  signal.

```yaml
quality_metrics:
  # at least 90 % of the cases scored on response_quality must score >= 4
  response_quality: { threshold: 4, min_pass_rate: 0.9 }
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
judging itself is acceptable for structural rubrics, weaker for quality. The judge reads every
turn of a case, so its context window must fit the conversation: `judge.max_tool_result_chars`
bounds each tool result, not the whole prompt.

## What to fix when a metric fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `contains` fails but the answer is right | wording variance | use `regex` or a judge; or make the prompt ask for the phrase explicitly |
| `not_contains` fails on a correct refusal | the forbidden word also appears in a legitimate answer (a status list, a quoted request) | forbid the specific leak (an id, an address), not a common word |
| a multi-turn `tool_calls` check misses an earlier call | `scope` is `final_turn` (the default) | set `expect.scope: all_turns` |
| `tool_calls` fails: wrong tool | tool descriptions overlap | sharpen docstrings; remove unused tools |
| `tool_calls` fails: no call | model does not call tools | on `openai-compatible`, check the model supports tools and the server parses them |
| `tool_calls` fails: refused by policy | the call is outside `api-policy.yaml`, or over one of its `limits` | the operation must be allowed by the policy's owner (`graph-agents-cli api allow`, a reviewed change) and declared in `API_CALLS`, a limit raised deliberately (`api limits`), or the tool must change |
| `groundedness` low | the answer adds unsupported claims | instruct the model to cite tool results; return structured tool output |
| `groundedness` low and the case has `judge_notes` about a cut | a tool result was longer than `judge.max_tool_result_chars` | raise the limit (or `null`), or return a smaller result |
| `task_success` low with a `reference` | the reference is too specific | rewrite the reference as the essential content, not exact wording |
| `max_latency_ms` fails | tool or model slow | measure with `run -v`; cache; reduce context |
| `error` status | server crash, timeout, judge exception | read `reasons`; fix generation before grading |
