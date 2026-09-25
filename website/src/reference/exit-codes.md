---
description: The exit-code scheme every graph-agents-cli command follows.
---

# Exit codes

One exit-code scheme for every command, so scripts, CI and coding agents can act on the result without parsing output.

<!--
WRITER BRIEF (lane: reference). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- The 0 / 1 / 2 / 3 table with every per-command case from the README table.
- Click usage errors are 2; a signal ends a command with 128+N (130 Ctrl-C, 143 SIGTERM) after the local server it started is stopped; network, file and parse errors print one line (GRAPH_AGENTS_CLI_DEBUG=1 shows the traceback).
- Per-command notes worth a line each: eval (gate), lint, run (awaiting approval is 0), approvals, secrets status, deploy --status, scaffold enhance and upgrade, api (its own Exit codes block in reference/cli.md).

Sources:
- README: "## Exit codes" (all); exit-code mentions across the README.
- CHANGELOG 0.2.0: Breaking "Exit codes are consistent".
- Code: CLI main.py (EXIT_* constants, _MainGroup.invoke, _environment_error), run/_signals.py, the ctx.exit / SystemExit calls in command modules; the Exit codes blocks in command help (rendered on reference/cli.md).

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- A few real exits: a command outside a project (3), eval run on the fake model (0), an unknown flag (2).

Link to at least: cli.md, ../guides/evaluation.md, ../getting-started/lifecycle.md
-->
