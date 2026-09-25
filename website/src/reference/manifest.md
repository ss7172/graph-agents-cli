---
description: graph-agents-cli-manifest.yaml: how a project was created and how the CLI treats it.
---

# Project manifest

`graph-agents-cli-manifest.yaml` records how a project was created and configures the CLI for it: its settings, the build that rendered it, the auth policy, the secrets allow-list and the environments.

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
- Where it lives, who writes it (create, scaffold enhance, scaffold upgrade, the api commands; rewritten without comments) and who reads it (build, deploy, secrets, infra check, upgrade, info).
- Every key with its meaning, taken from the template and the code (not from the README): create_params (registry and the other create settings), cli_version, cli_build (id, commit, template_digest), generated_at, auth_policy, auth_policy_implemented, api_policy, secrets (keys, owner), environments.<env>.context, and anything else present.
- The keys users edit by hand (environments.<env>.context, secrets.keys, auth_policy_implemented) and the ones they should not.
- Related file: .github/agent.env (IMAGE_REPOSITORY, RELEASE_NAME, CHART_PATH, RUNTIME, CD, GRAPH_AGENTS_CLI_SPEC; data, never sourced).
- An annotated example from a fresh scratch project.

Sources:
- README: "### Upgrading a project" (cli_version, cli_build, template_digest, generated_at); "## Quick start" (create_params.registry); "## Authentication" (auth_policy); "### `custom`" (auth_policy_implemented); "## Secrets" (secrets.keys, secrets.owner); "## Environments and CD modes" (environments.<env>.context); "### Required GitHub settings" (the agent.env paragraph).
- CHANGELOG 0.2.0: Added "The manifest records the build that rendered the project".
- Code: BASE/_shared/graph-agents-cli-manifest.yaml, CLI scaffold/utils/{manifest,build_record,generation_metadata}.py, _project.py, deploy/_config.py, api/_files.py.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- cat the manifest of a fresh scratch project; graph-agents-cli info --json.

Link to at least: ../guides/upgrading.md, ../guides/secrets.md, ../guides/deploy.md, ../guides/cicd.md
-->
