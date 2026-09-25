---
description: How graph-agents-cli relates to google-agents-cli: what it keeps, where it goes further, where it is behind.
---

# Compared with google-agents-cli

How graph-agents-cli relates to the project it forked: what it keeps, where it goes further, where it is behind, and what is out of scope.

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
- The fork: started from google-agents-cli 1.6.1 with the Google Cloud specific parts removed (see NOTICE); keeps the lifecycle and the scaffold engine; LangGraph on any Kubernetes instead of ADK on Google Cloud; compared as of google-agents-cli 1.7.0 (September 2026).
- A comparison table (area / graph-agents-cli / google-agents-cli), then the 'further', 'behind' and 'out of scope' lists.
- Update the README's 'Maturity' bullet: this documentation site now exists (published once the owner enables it), tags v0.1.0 and v0.2.0 exist, PyPI publication is still pending.
- Upstream fixes are ported by hand (CONTRIBUTING's upstream-sync process); remote templates still skip symlinks.

Sources:
- README: intro (the fork paragraph); "## Compared with google-agents-cli" (all).
- NOTICE (attribution and the list of modifications).
- CONTRIBUTING.md: "## Upstream sync".
- KNOWN_ISSUES: KI-071, KI-078, KI-091, KI-092, KI-093.
- Upstream facts: a google/agents-cli checkout via git show HEAD:<path> (for example CHANGELOG.md, docs/src/reference/about.md); check the version claims there.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Upstream version claims against the upstream checkout's tags and changelog.

Link to at least: ../index.md, known-issues.md, changelog.md
-->
