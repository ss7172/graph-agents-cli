---
description: Deploy a graph-agents-cli agent to any Kubernetes cluster with Helm: environments, the chart, local clusters, rollouts and an external database.
---

# Deploy to Kubernetes

Take the agent to any Kubernetes cluster with Helm: the three environments, the chart, local clusters, direct deploys, rollouts and rollback, and an external database.

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
- Environments dev / staging / prod: values-<env>.yaml, namespace <name>-<env>, Secret <name>-app, an Argo CD Application; dev bundles Postgres (and Redis for langgraph-server) without a Gateway; staging and prod expect an external database and an HTTPRoute or Ingress.
- What deploy does in each CD mode (the deploy column of the mode table); the CI side lives in cicd.md.
- The registry: create --registry, else the git origin (ghcr.io/<owner>), else the ghcr.io/CHANGE-ME placeholder that build and deploy refuse (exit 3); scaffold enhance --registry updates the manifest, the chart and .github/agent.env; build --tag/--registry/--push/--dry-run.
- The rules deploy and secrets apply follow, one short section each: env file, kube context, order of checks, settings that cannot work, rollout (helm upgrade --install --wait, --timeout, diagnostics, --atomic rollback of its own revision, refusal while another helm operation holds the release), the Secret after a failure, same image, image tags, local clusters, protected environments, chart dependencies.
- deploy --status, --restart, --dry-run; GH_HOST for GitHub Enterprise Server.
- The chart: pod security, probes, resources and the values worth knowing (a table: value, default, purpose), including networkPolicy and the worked examples/networkpolicy.yaml.
- External database: the least-privileged role SQL, a verify-full DSN, mounting the CA, the TLS warnings, pg_hba hostssl.
- infra check for cluster prerequisites (GitHub settings in cicd.md).
- Limitations: concurrent deploys, a database that stops answering, the Docker Hub subcharts, and the deploy-related KI entries.

Sources:
- README: "## Environments and CD modes" (all), "### Chart", "### External database"; "## Quick start" (the local-cluster and registry paragraph); "## Commands" rows build, deploy, infra check; "## Known limitations" bullets "Concurrent deploys to one release", "A database that stops answering ...", "The Bitnami subcharts ...", "Rolling upgrades from an older build" (link upgrading.md).
- CHANGELOG 0.2.0: Breaking "`deploy` and `secrets apply` outside `dev` need an explicit env file and kube context", "Chart defaults are stricter", "Chart: bounded shutdown and a separate metrics Secret", "`deploy` refuses more outside `dev`"; Added "Database outages", "deploy: a failed rollout that is rolled back ..."; "#### Upgrading a running deployment".
- KNOWN_ISSUES: KI-017, KI-028 to KI-033, KI-057, KI-072, KI-074 to KI-077, KI-081, KI-082.
- Skills: graph-agents-cli-deploy/SKILL.md, references/kubernetes.md.
- Code: CLI deploy/*.py (cmd_deploy, _modes, _preflight, _kube, _image, _values, _config, local_load, gitops), dev/cmd_build.py, infra/checks.py; CHART values.yaml, values-{dev,staging,prod}.yaml, templates/*, examples/networkpolicy.yaml, Chart.yaml.
- Upstream model: docs/src/guide/deployment.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- In a scratch project created with --registry localhost/dev: build --dry-run, deploy --env dev --dry-run (prints commands and rendered manifests); a kind cluster is optional, say what you ran.

Link to at least: cicd.md, secrets.md, upgrading.md, security.md, ../reference/cli.md#graph-agents-cli-deploy
-->
