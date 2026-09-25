---
description: Apply, check and rotate the allow-listed Kubernetes Secret of a graph-agents-cli agent without printing a value.
---

# Secrets

Keep application secrets in one allow-listed Kubernetes Secret per environment, and apply, check and rotate them without a value ever reaching a values file, a log or a command line.

<!--
WRITER BRIEF (lane: guides_ops). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- The allow-list (secrets.keys in the manifest): the provider key, JUDGE_API_KEY, POSTGRES_DSN (or DATABASE_URI and REDIS_URI), API_KEY (shared-bearer only), LANGSMITH_API_KEY, each bearer API's token_env; add others such as METRICS_TOKEN or PRINCIPAL_HASH_SALT.
- The procedure as numbered steps: env file; secrets apply (server-side apply through a 0600 temp file, no value on a command line or in last-applied-configuration; keys the env file leaves out are kept; single-line values); API_KEY rules and --rotate-api-key; secrets status (present, missing required, missing optional, unexpected; which keys are required); rotation then deploy --restart.
- The <name>-metrics Secret for the ServiceMonitor.
- The env-file rule (.env.<env>; only dev falls back to .env; otherwise exit 3).
- Who runs it in argocd and helm-push modes (secrets.owner, from a workstation; CI never holds app secrets); --force-conflicts ownership (do not let External Secrets manage the same keys).
- Limitations: the secrets-related KI entries.

Sources:
- README: "## Secrets" (all); "## Environments and CD modes" bullet "Env file"; "## Commands" rows secrets apply, secrets status.
- CHANGELOG 0.2.0: Breaking "A live `API_KEY` is never replaced implicitly", "New projects list `API_KEY` in `secrets.keys` only under `shared-bearer`".
- KNOWN_ISSUES: KI-038, KI-075, KI-084, KI-085.
- Skills: graph-agents-cli-deploy/references/secrets.md.
- Code: CLI secrets/cmd_secrets.py, secrets/_apply.py, secrets/_required.py, deploy/_preflight.py; BASE/_shared/graph-agents-cli-manifest.yaml (secrets block).

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- secrets apply --help, secrets status --help; the --dry-run variants in a scratch project (check whether they need a cluster).

Link to at least: deploy.md, cicd.md, ../reference/manifest.md, security.md
-->
