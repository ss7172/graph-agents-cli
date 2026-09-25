---
description: Install graph-agents-cli from its release tag, add the skills to your coding agents and check your environment.
---

# Installation & setup

Install the CLI from its pinned release tag, install the skills into your coding agents, and check that your machine is ready before you create a project.

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
- Prerequisites table (tool / needed for): Python 3.12 or 3.13, uv; Node.js only for the skills installer (setup falls back to a plain copy without it); helm, kubectl, a Docker-compatible docker CLI and git to deploy; gh for argocd or GitHub-hosted CD; a tool missing from PATH makes deploy exit 2.
- Install: uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0. Warning admonition: PyPI publication is pending, and a package named graph-agents-cli on an index is not this project until the release workflow publishes it.
- Optional extras: a2a (run --mode a2a) and langsmith (eval submit): uv tool install 'graph-agents-cli[a2a,langsmith] @ git+https://github.com/ss7172/graph-agents-cli@v0.2.0'.
- --version build ids: 0.2.0 (release), 0.2.0+g<commit> (other commits), .dirty suffix; info shows the full commit. Installing a build from a checkout: link CONTRIBUTING on GitHub.
- setup: npx skills add from this repository at the running release's tag (#v0.2.0), default branch for a development build; fallback to the wheel's bundled copy, then a plain copy into ~/.agents/skills (./.agents/skills with --workspace); --skills-source (path, owner/repo, URL#ref); --agent (claude-code, cursor, ... or all); --dry-run; --dev; the Antigravity mirror (~/.gemini/config/skills, ~/.gemini/antigravity-cli/skills). Tabs for 'all detected agents' / 'one agent' / 'this workspace only' work well.
- update: refreshes the skills, then upgrades the CLI to the latest GitHub release (best effort) and moves the skills to that release's tag.
- login: what it checks (provider key or OPENAI_BASE_URL, API_KEY under shared-bearer, jwt key and local token, LANGSMITH_API_KEY when tracing, a .env other users can read, kubeconfig); --write-env (prompts without echo, generates API_KEY, fills blank KEY= lines in place, leaves .env at 0600); --profile disconnected; --cluster; --status (exit 0); --json; exit 1 on a failed check. The CLI stores no credentials.
- Update check at most every 12 hours; GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1 turns it off. GRAPH_AGENTS_CLI_INSTALL_SPEC (mirror, wheel, index; {version} placeholder, refusals exit 3): one paragraph here, full rules in reference/environment.md.

Sources:
- README: "## Install" (all); "## Quick start" (the login line); "## Commands" rows setup, update, login, info; "## Known limitations" bullet "The repository has no release tags yet" is STALE: tags v0.1.0 and v0.2.0 exist and the v0.2.0 GitHub release is published (checked 2026-09-24 with git tag and gh release list), so do not repeat it.
- CHANGELOG 0.2.0: Breaking "Installation moved to a pinned git reference", "`CLI_VERSION_PIN` is now `GRAPH_AGENTS_CLI_SPEC`"; Added "The manifest records the build that rendered the project".
- CONTRIBUTING.md: "### Installing a build from a checkout" (link to it on GitHub, do not copy it).
- Skills: skills/graph-agents-cli-workflow/references/commands.md.
- Code: CLI setup/cmd_setup.py, setup/cmd_update.py, setup/cmd_auth.py (login), setup/_antigravity.py, scaffold/utils/version.py (install spec, update check), _build.py (--version), info/cmd_info.py, _skills_check.py; pyproject.toml [project.optional-dependencies].
- Upstream model: docs/src/guide/getting-started.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- graph-agents-cli --version; setup --help; setup --dry-run; update --help; login --help; login --status inside a fresh scratch project.

Link to at least: quickstart.md, ../reference/environment.md, ../reference/cli.md#graph-agents-cli-setup, tutorial-coding-agent.md, ../guides/offline.md
-->
