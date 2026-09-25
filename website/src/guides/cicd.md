---
description: How changes reach a cluster in each graph-agents-cli CD mode, what the generated GitHub Actions workflows do, and the settings they need.
---

# CI/CD

How changes reach a cluster in each CD mode (`skip`, `helm-push`, `argocd`), what the generated GitHub Actions workflows do, and the repository settings they need.

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
- --cd at create time (scaffold enhance --cd later); the full mode table (deploy column and CI column).
- The generated workflows: pr_checks (ruff, tests, the eval gate; real model through a key secret or repository variables), staging (helm-push: self-hosted runner; argocd: PR with auto-merge, newer PRs supersede older ones), promote-to-prod (the production environment gate); rollout verification through /health and /ready.
- The Argo CD flow: deploy opens a PR changing only image.tag in values-<env>.yaml (branch deploy/<env>/<short sha>, git plumbing from origin/main, your checkout untouched); production is a PR merged by a human after code-owner review, then a manual sync; the deployment/argocd Applications (repoURL) applied once.
- Required GitHub settings (every README bullet), as a checklist: environments and reviewers, branch protection, GH_PR_TOKEN and why, DEPLOY_KUBECONFIG as an environment secret, registry credentials, Argo CD credentials, the optional provider key.
- .github/agent.env: the allowed names, read as data and never sourced.
- infra check reports these settings when gh is logged in (or GITHUB_TOKEN is set).
- Every generated job sets GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1 and installs the CLI pinned by GRAPH_AGENTS_CLI_SPEC.
- Limitations: actions pinned by tag, argocd promotion edge cases, thin post-deploy verification, no provisioning.

Sources:
- README: "## Environments and CD modes" (mode table); "### Required GitHub settings (`helm-push`, `argocd`)" (all); "## Secrets" (last paragraph); "## Known limitations" bullet "The generated workflows reference actions by version tag".
- CHANGELOG 0.2.0: Breaking "`helm-push` reads `DEPLOY_KUBECONFIG` ...", "`.github/agent.env` is data, not shell", "`CLI_VERSION_PIN` is now `GRAPH_AGENTS_CLI_SPEC`".
- KNOWN_ISSUES: KI-035, KI-036, KI-037, KI-073, KI-078, KI-079, KI-080, KI-083.
- Skills: graph-agents-cli-deploy/references/gitops.md, references/github-settings.md.
- Code: BASE/python/.github/workflows/pr_checks.yaml, BASE/python/.github/agent.env, BASE/python/.github/CODEOWNERS; K8S/.github/workflows/staging.yaml, promote-to-prod.yaml, K8S/.github/agent.env; K8S/deployment/argocd/*.yaml; CLI deploy/gitops.py, deploy/_modes.py, infra/checks.py, scaffold/commands/enhance.py.
- Upstream model: docs/src/guide/cicd.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- create scratch projects with --cd argocd and --cd helm-push and read the rendered workflows; deploy --env staging --dry-run in argocd mode (contacts no cluster).

Link to at least: deploy.md, secrets.md, evaluation.md, ../reference/manifest.md
-->
