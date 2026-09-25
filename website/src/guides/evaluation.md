---
description: Evaluate a graph-agents-cli agent with datasets, deterministic checks and LLM judges, and enforce the gate in CI.
---

# Evaluation

Measure the agent with datasets, deterministic checks and LLM judges, and enforce a gate whose exit code CI and coding agents act on.

<!--
WRITER BRIEF (lane: guides_build). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- eval run = eval generate + eval grade; every case in tests/eval/datasets/*.json goes to POST /chat (a multi-message case on one thread); traces in artifacts/traces/, results in artifacts/grade_results/.
- The gate, precisely: every case accounted for, every deterministic expect check and mandatory judge passed, each quality_metrics entry at or above its min_pass_rate over the cases scored on it.
- A dataset case example (from TPL/tests/eval/datasets/basic-dataset.json) and eval_config.yaml; full schema via the eval skill's dataset_schema reference.
- expect.contains / not_contains (case-insensitive by default, case_insensitive: false), scope: all_turns.
- Judges see every turn; judge.max_tool_result_chars (default 50000, null never cuts); {transcript}; --judge-provider/--judge-model and JUDGE_* variables.
- The fake model: the two warnings and 'plumbing check only' (real text captured 2026-09-24; re-run to confirm).
- eval run --url against a deployed agent: tools run for real; the warning; GRAPH_AGENTS_CLI_API_KEY; model: null in results; masked credentials in URLs.
- Approvals in eval: approvals instructions with match, what happens to unmatched gates, cleanup statuses, expect.approvals and expect.no_approvals, GRAPH_AGENTS_CLI_APPROVER_API_KEY for role: gates.
- The other commands: compare (--fail-on-regression, --json), analyze (--judge), submit (LangSmith; langsmith extra), metric list.
- Exit codes for eval (0 gate met, 1 failed case or quality metric, 2 error/missing case or unreachable agent).
- In CI: pr_checks runs the gate; a real provider through the key secret or MODEL_PROVIDER/MODEL_NAME repository variables.

Sources:
- README: "## Evaluation" (all); "## Commands" rows eval *; "## Exit codes" (eval parts); "### Required GitHub settings" (last bullet); "## Compared with google-agents-cli" (evaluation bullets, for what is not there).
- CHANGELOG 0.2.0: Breaking "Eval gates can change result"; Added "Eval approvals", "eval: judges of a multi-turn case see every earlier turn".
- KNOWN_ISSUES: KI-027, KI-065 to KI-071.
- Skills: graph-agents-cli-eval/SKILL.md, references/dataset_schema.md, references/metrics-guide.md.
- Code: CLI eval/*.py (cmd_run, cmd_generate, cmd_grade, gate, checks, dataset, config, transcript, _judge, _judge_runner, cmd_compare, cmd_analyze, cmd_submit, cmd_metric); TPL/tests/eval/eval_config.yaml, TPL/tests/eval/datasets/basic-dataset.json; BASE/python/.github/workflows/pr_checks.yaml.
- Upstream model: docs/src/guide/evaluation.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- eval run in a scratch project on the fake model; eval metric list; eval compare on two results files; eval analyze.

Link to at least: approvals.md, cicd.md, ../reference/exit-codes.md, ../reference/cli.md#graph-agents-cli-eval-run
-->
