---
description: Run the whole graph-agents-cli lifecycle without internet access: on-network models, mirrors, and checks for hosted dependencies.
---

# Offline profile

Run the whole lifecycle without internet access (the CLI calls this the disconnected profile): an on-network model, mirrored dependencies and images, and checks that fail on any hosted dependency.

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
- 'Runs locally' (orchestration on your machine) versus 'runs disconnected' (no internet at all).
- The profile as a checklist: openai-compatible with OPENAI_BASE_URL at an on-network server (vLLM, TGI, Ollama) and a tool-capable model, the judge through JUDGE_*; runtime fastapi (the LangGraph Server image needs a licence check); a private index (UV_INDEX_URL, install --locked); mirrored base images; vendored subcharts under deployment/helm/<name>/charts/; tracing off or OTLP to an in-cluster collector, no LangSmith; GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1; skills from the wheel bundle; the CLI from a mirror (GRAPH_AGENTS_CLI_INSTALL_SPEC); cd: skip with direct deploys unless an on-network GitHub Enterprise Server runs Actions (GH_HOST).
- login --profile disconnected and infra check --profile disconnected: what each fails on (hosted model or judge, LANGSMITH_API_KEY, tracing without an OTLP endpoint, GitHub-hosted runner labels, langgraph-server, a registry that is not on-network). Show real output.
- Use 'offline' in the title and first sentence only; the CLI, its flags and the skills say 'disconnected'.

Sources:
- README: "## Disconnected profile" (all); "## Install" (GRAPH_AGENTS_CLI_INSTALL_SPEC, setup fallbacks); "## Production checklist" (mirror/vendor item); "## Environments and CD modes" (bullet "Chart dependencies", the GH_HOST sentence).
- Skills: graph-agents-cli-deploy/SKILL.md (disconnected profile); graph-agents-cli-eval/SKILL.md (local versus disconnected).
- Code: CLI setup/cmd_auth.py (profile checks), infra/checks.py (profile), scaffold/utils/version.py, skills/_bundle.py; TPL/app/app_utils/model.py (openai-compatible).

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- login --profile disconnected --status and infra check --profile disconnected in a scratch project; paste the real report.

Link to at least: ../getting-started/installation.md, deploy.md, observability.md, ../reference/environment.md
-->
