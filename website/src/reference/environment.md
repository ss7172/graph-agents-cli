---
description: Every environment variable graph-agents-cli reads and every setting of the generated service, with defaults.
---

# Environment variables

Every environment variable the CLI reads and every setting of the generated service, with its default and a link to the guide that explains it.

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
- CLI variables (table: name, default, meaning): GRAPH_AGENTS_CLI_INSTALL_SPEC ({version}, refusals, exit 3), GRAPH_AGENTS_CLI_NO_UPDATE_CHECK, GRAPH_AGENTS_CLI_API_KEY, GRAPH_AGENTS_CLI_APPROVER_API_KEY, GRAPH_AGENTS_CLI_RUN_PORT, GRAPH_AGENTS_CLI_DEBUG, GRAPH_AGENTS_CLI_DISABLE_OVERRIDES, plus every other variable the CLI code reads (GH_HOST, GITHUB_TOKEN, KUBECONFIG, ...): enumerate from the code, not the README.
- Service settings grouped (runtime and model; auth; guardrails and limits; database; logging and metrics; tracing; A2A; CORS; retention; APP_ENV): name, default, one-line meaning, link to the guide. TPL/.env.example is the contract; take defaults from the code that parses them.
- Parse rules: a value that does not parse stops the app at startup naming every bad variable; TRACING_ENABLED is on only for true, yes or 1; APP_ENV is dev only when exactly dev; .env is read below the process environment.
- The AUTH_JWT_* table lives in guides/authentication.md: list the names here and link.
- Optional: generating the service table from TPL/.env.example with a hook would keep it exact; that touches shared files (mkdocs.yml, hooks/), so propose it to the scaffold owner instead of editing them.

Sources:
- README: "## Commands" (the CLI environment variables paragraph); "## Install" (INSTALL_SPEC, NO_UPDATE_CHECK); "### Endpoints" (every Behaviour bullet names settings; the closing paragraph "Every setting has a default in code ..."); "## Authentication" (common settings); "### `jwt`" (variable table); "## Disconnected profile".
- CHANGELOG 0.2.0: Breaking "One message cap for every surface"; Added "A2A" (A2A_DESCRIPTION), "Runtime guardrails".
- Code: TPL/.env.example (primary); TPL/app/app_utils/{limits,db,telemetry,metrics,auth,model,a2a,chat,middleware}.py (parsing and defaults); CLI main.py, _runner.py, run/_local_server.py, scaffold/utils/version.py, eval/_client.py.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- grep -rn 'environ' src/graph_agents_cli --include='*.py' (outside scaffold/) to enumerate what the CLI reads; compare every default with the parsing code.

Link to at least: ../guides/authentication.md, ../guides/observability.md, http-api.md, ../guides/offline.md
-->
