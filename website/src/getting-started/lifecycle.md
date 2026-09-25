---
description: The stages of a graph-agents-cli project from create to operate, the commands in each, and how a change reaches production.
---

# The lifecycle

The concepts behind every project: five stages from create to operate, the commands that belong to each, and how a change travels from your laptop to production.

<!--
WRITER BRIEF (lane: getstarted). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- Concepts, not an animation: reuse the .gac-lifecycle component from the home page (markup in website/src/index.md, styles in stylesheets/custom.css).
- A stage-by-stage command table covering every top-level command (setup/update/login before create; create/scaffold; install, run, playground, api, lint, info; eval *; build, secrets, deploy, infra check; deploy --status/--restart, approvals, scaffold upgrade; extension), each linked to its reference/cli.md anchor (#graph-agents-cli-<command>). Descriptions from the commands' real short help.
- The exit-code contract (0 ok, 1 refused or failed gate, 2 tool failure, 3 configuration error) as what CI and coding agents read; link reference/exit-codes.md.
- dev, staging and prod, and the three CD modes at a glance (one short table); link guides/deploy.md and guides/cicd.md.
- 'Runs locally' versus 'runs disconnected' (one paragraph); link guides/offline.md.
- What the project owns (api-policy.yaml, its code, the manifest) versus what the CLI regenerates (templates via scaffold upgrade).

Sources:
- README: intro (before "## Install"); "## Commands" (table, for the stage mapping only: flags come from reference/cli.md); "## Disconnected profile" (first paragraph); "### The policy's lifecycle" (intro paragraph); "## Environments and CD modes" (intro and mode table).
- Skills: skills/graph-agents-cli-workflow/SKILL.md (phases); references/terminology.md.
- Code: CLI main.py (every registered command and its short help).
- Upstream model: docs/src/guide/lifecycle.md (concepts only; no JavaScript).

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- graph-agents-cli --help for the command list and short helps.

Link to at least: quickstart.md, ../reference/cli.md, ../reference/exit-codes.md, ../guides/deploy.md, ../guides/cicd.md, ../guides/offline.md
-->
