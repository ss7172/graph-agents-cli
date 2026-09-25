---
description: Upgrade a graph-agents-cli project to a newer CLI build with scaffold upgrade, keep your edits, and migrate from 0.1.0.
---

# Upgrading projects

Move a project to a newer CLI build with `scaffold upgrade`, which merges template changes into your code without losing your edits, and handle the special cases: builds between releases, 0.1.0 projects and running deployments.

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
- How it works: the old templates re-rendered with the build that created the project, a 3-way merge, conflicts listed or resolved with -i, --dry-run to preview, -y to apply, a backup first.
- The manifest's cli_version, cli_build (id, commit, template_digest) and generated_at; the three cases (older release, same version, no cli_build).
- --baseline-ref: commit or tag, <clone>@<commit>, a checkout or wheel path, a full install spec; it must render the manifest's cli_version (exit 3); finding the commit with git log --before; the warning when most files would keep their content.
- --baseline authentic | current, and why current is no substitute for a 0.1.0 project.
- Builds with uncommitted changes (.dirty) cannot be rebuilt; every release is tagged v<version>.
- Exit codes: 2 when the baseline cannot be built (uvx missing or failing), 3 for manifest, install-spec or --baseline-ref reasons.
- scaffold enhance (a settings change) versus upgrade (a new version); both rewrite the manifest without comments; install after enhance --runtime.
- Migrations: CHANGELOG's three #### upgrade sections, and the rolling-upgrade caveat (Recreate rollout or one replica).
- Files upgrade never touches (api-policy.yaml and those the scaffold skill lists).
- GRAPH_AGENTS_CLI_INSTALL_SPEC with {version} for mirrors.

Sources:
- README: "### Upgrading a project" (all); "## Install" (the GRAPH_AGENTS_CLI_INSTALL_SPEC paragraph); "## Commands" rows scaffold upgrade, scaffold enhance; "## Known limitations" bullets "Rolling upgrades from an older build", "`scaffold enhance` ... and `scaffold upgrade` ...", "Upgrading a 0.1.0 project ...", "A project made by a build between releases ...".
- CHANGELOG 0.2.0: "### Breaking changes and migration" with "#### Upgrading a running deployment", "#### Upgrading a project created with 0.1.0", "#### Upgrading a project made by a pre-release 0.2.0 build"; Added "The manifest records the build that rendered the project", "`scaffold upgrade --baseline-ref REF`".
- KNOWN_ISSUES: KI-040, KI-041, KI-093.
- Skills: graph-agents-cli-scaffold/SKILL.md (authentic-baseline rule, files upgrade never touches).
- Code: CLI scaffold/commands/upgrade.py, scaffold/commands/enhance.py, scaffold/utils/{upgrade,merge3,merge,build_record,generation_metadata,backup,manifest,version}.py, _build.py.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- scaffold upgrade --dry-run in a fresh scratch project (expect 'already at version'); scaffold upgrade --help; scaffold enhance --help.

Link to at least: ../reference/manifest.md, ../reference/changelog.md, deploy.md, ../reference/exit-codes.md
-->
