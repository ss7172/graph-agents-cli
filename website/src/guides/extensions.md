---
description: Override or add graph-agents-cli commands from an extension repository or a local path (experimental).
---

# Extensions

Override built-in commands or add new ones from an extension repository or a local path, with an explicit trust decision for new third-party code. Extensions are experimental.

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
- extension add REFERENCE [--global] [--ref] [-i] [-y], list, remove NAME, update [NAME]; project versus global scope; the trust prompt, -y, never prompting without a terminal, update asking only when the code changed.
- What an extension can do: add commands, override a top-level command or one subcommand (group.sub), never a whole group; overriding scaffold.create also takes over create; a command blocked by a CLI version requirement; the warning line printed when extension commands apply.
- The extension manifest: schemas/graph-agents-cli-extension-v1alpha1.schema.json (fields, the CLI version requirement).
- GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1: ignores overrides; set in every generated CI and CD job. Project-scope extensions run with the trust of the repository you are in.
- Authoring: a minimal worked example, adapted from upstream's extension guides.

Sources:
- README: "## Commands" row extension; the CLI environment variables paragraph (GRAPH_AGENTS_CLI_DISABLE_OVERRIDES).
- CHANGELOG 0.2.0: Breaking "`extension add` / `update` without a terminal never prompts".
- KNOWN_ISSUES: KI-090 (check which items concern extensions).
- Skills: graph-agents-cli-workflow/references/extension.md.
- Code: CLI extension/*.py (cmd_extension_add/list/remove/update/group, _manifest, _schema, _loader, _resolver, _trust, _refs, _sync, _overrides, _compat, _spec, _paths), main.py (_MainGroup._apply_extensions), _trust.py; schemas/graph-agents-cli-extension-v1alpha1.schema.json.
- Upstream model: docs/src/guide/extensions/{index,using,authoring}.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Build a local extension in a scratch dir that overrides one command, extension add <path> -y, run it, list, remove. Global scope writes to the user config: point it at a scratch location (see extension/_paths.py) instead of your real home.

Link to at least: ../reference/cli.md#graph-agents-cli-extension, ../reference/environment.md, cicd.md
-->
