# Writing the documentation site

The public documentation is a MkDocs Material site: `mkdocs.yml` here, pages in `src/`, build
hooks in `hooks/`, theme overrides in `overrides/`, build output in `build/` (git ignored).
This file is outside `docs_dir` and is never published.

```bash
uv run --group docs mkdocs serve -f website/mkdocs.yml            # preview
uv run --group docs mkdocs build --strict -f website/mkdocs.yml   # what CI runs
```

Generated or included pages need no edits: the CLI reference comes from the Click commands
(`hooks/cli_reference.py`), the Skills page from `skills/*/SKILL.md`
(`hooks/skills_reference.py`), and the Known issues and Changelog pages include
`KNOWN_ISSUES.md` and `CHANGELOG.md` from the repository root.

## Rules for every page

- **Shared files.** `mkdocs.yml`, `hooks/`, `overrides/`, `src/stylesheets/` and
  `src/assets/` serve every page: change them deliberately and check the whole site.
- **Facts come from the code.** Run the commands (`--help`, and the real flow in a scratch
  directory) with `MODEL_PROVIDER=fake` and `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`; paste real
  output. Start local servers on free ports (`run` honours `GRAPH_AGENTS_CLI_RUN_PORT`,
  `playground` takes `--port`) and stop them afterwards.
- **Public sources only.** Never read or cite the repository's `docs/` directory (private and
  git ignored). Upstream pages: `git show HEAD:<path>` in a google/agents-cli checkout
  (tracked files only).
- **Generic.** No consumer names, no domain-specific examples beyond the orders / payments
  examples the site already uses. No exploit-level detail: describe risks and controls.
- **Shape.** One idea per section, short paragraphs, tables for reference data, admonitions
  (`!!! warning`) for warnings, content tabs (`=== "OpenAI"`) for alternatives, a grid of
  cards for "next steps". Keep the H1 and the front-matter `description`.
- **Links.** Relative links to `.md` files (`../reference/cli.md#graph-agents-cli-deploy`).
  `mkdocs build --strict` fails on a missing page or anchor; CLI anchors are
  `#graph-agents-cli-<command>[-<subcommand>]`. Link a command's flags to the CLI reference
  instead of copying the flag list.
- **Install commands.** Install from PyPI without a version (`uv tool install
  graph-agents-cli`); never write `graph-agents-cli==X.Y.Z`, which no check keeps current. A
  pinned install names the current release tag (`git+https://github.com/ss7172/graph-agents-cli@vX.Y.Z`);
  `hooks/version.py` fails the build when a page names another version, so release bumps
  cannot leave stale commands behind.
- **Check before a pull request:** `mkdocs build --strict` (above) passes, and `uv run --group docs mkdocs serve -f website/mkdocs.yml -a 127.0.0.1:<port>`
  (any free port) looks right at desktop and phone widths.

## Components you can use

Defined in `src/stylesheets/custom.css`; see `src/index.md` for markup.

| Component | Markup |
|---|---|
| Grid cards | `<div class="grid cards" markdown>` + a `-   ` list (Material) |
| Three cards per row | `<div class="grid cards gac-cols-3" markdown>` (use for exactly three cards) |
| Numbered step cards | `<div class="grid cards gac-steps" markdown>` |
| Lifecycle diagram | `<ol class="gac-lifecycle" markdown="block">` with `<li markdown="block">` items (see `index.md`) |
| Terminal frame | `<div class="gac-terminal" markdown>` around a fenced block |
| Intro paragraph | `<p class="gac-lede">…</p>` under the H1 |
| Pill badge | `<span class="gac-pill">…</span>` (`gac-pill--brand`, `gac-pill--accent`) |
| Icons | `:material-<name>:` / `:octicons-<name>-16:` (pymdownx.emoji, bundled SVG) |
| Task list | `- [ ] item` (pymdownx.tasklist) |
| Diagrams | inline SVG, or a ` ```mermaid ` fence (loads Mermaid from a CDN at view time: use sparingly) |
| Tables that line up | `<div class="gac-tables" markdown>` around consecutive tables of one shape (full width, same first column) |
| Command output | a ` ```text ` fence, untitled or `title="Output"`: `hooks/output_blocks.py` labels it "Output" and drops its copy button |
| A shorter sidebar | `toc_depth: 2` in a page's front matter (`hooks/toc_depth.py`) |

## Where the 0.2.0 README went

Until 0.2.0 the repository README held the whole documentation. Every `##` and `###`
heading of that README (`README.md` at commit 7ece9d2) maps to the page(s) that now carry its
facts: "primary" pages hold the full facts, "also" pages summarize and link to the primary.

| README heading (line) | Primary page(s) | Also |
|---|---|---|
| `# graph-agents-cli` intro (1-31) | `index.md` (pitch, generic, skills); `reference/comparison.md` (fork, NOTICE) | `getting-started/lifecycle.md` |
| `## Install` (33) | `getting-started/installation.md` | `reference/environment.md` (GRAPH_AGENTS_CLI_INSTALL_SPEC, NO_UPDATE_CHECK); `guides/upgrading.md` (install spec `{version}`); `guides/offline.md` (mirror, wheel skills) |
| `## Quick start` (83) | `getting-started/quickstart.md` | `guides/authentication.md` (jwt dev-token block); `getting-started/tutorial-manual.md` (create options); `guides/deploy.md` (local cluster, registry placeholder paragraph) |
| `## Commands` (145) | `reference/cli.md` (generated: every command and flag) | `getting-started/lifecycle.md` (commands by stage); `reference/environment.md` (the "CLI environment variables" paragraph); each guide for its commands |
| `### Upgrading a project` (196) | `guides/upgrading.md` | `reference/manifest.md` (cli_version, cli_build, template_digest) |
| `## The generated service` (240) | `guides/develop.md` (tree, runtimes) | `reference/http-api.md` |
| `### Endpoints` (267): route table | `reference/http-api.md` | `guides/develop.md` (summary table) |
| `### Endpoints` bullets One run per thread, Limits, Thread ids, Timeouts, Step limit, A valid history, Tool arguments, Errors, Run records | `reference/http-api.md` | `guides/security.md` (Thread ids); `guides/observability.md` (Run records) |
| `### Endpoints` bullets Retention, Logging, Tracing | `guides/observability.md` | `reference/environment.md` |
| `### Endpoints` bullet Database | `guides/deploy.md` (External database section) | `reference/environment.md` (DB_POOL_*) |
| `### Endpoints` bullets Settings from `.env`, CORS, closing paragraph (defaults, startup parse rules) | `reference/environment.md` | `guides/develop.md` |
| `## Authentication` (381) | `guides/authentication.md` | `reference/environment.md` (AUTH_* names) |
| `` ### `shared-bearer` (default) `` (400) | `guides/authentication.md` | `guides/secrets.md` (API_KEY generation) |
| `` ### `jwt` `` (408) | `guides/authentication.md` (full AUTH_JWT_* table) | `reference/environment.md` (names, link) |
| `` ### `custom` `` (442) | `guides/authentication.md` | `reference/manifest.md` (auth_policy_implemented) |
| `` ## Outbound API policy (`api-policy.yaml`) `` (486) | `guides/api-policy.md` (concepts, access, auth modes, per-user authorization, limits, API_CALLS, lint) | `reference/api-policy-schema.md` (every key, the fail-closed matching rules) |
| `` ### Human approval of calls (`approval`) `` (613) | `guides/approvals.md` | `reference/api-policy-schema.md` (approval schema); `reference/http-api.md` (approval routes, A2A); `guides/evaluation.md` (eval approvals) |
| `### The policy's lifecycle` (785) | `guides/api-policy.md` | `getting-started/tutorial-manual.md` (worked example) |
| `## Evaluation` (843) | `guides/evaluation.md` | `getting-started/quickstart.md` (one paragraph) |
| `## Environments and CD modes` (897): environments, rules, deploy flags, local clusters | `guides/deploy.md` | `getting-started/lifecycle.md` (at a glance) |
| `## Environments and CD modes` (897): CD mode table, CI column, GH_HOST | `guides/cicd.md` | `guides/deploy.md` (deploy column) |
| `### Chart` (982) | `guides/deploy.md` | `guides/observability.md` (metrics values); `guides/security.md` (pod security, NetworkPolicy) |
| `### External database` (1025) | `guides/deploy.md` | `guides/security.md` (checklist item) |
| `## Secrets` (1049) | `guides/secrets.md` | `guides/cicd.md` (who runs `secrets apply` in argocd / helm-push) |
| `` ### Required GitHub settings (`helm-push`, `argocd`) `` (1084) | `guides/cicd.md` | `reference/manifest.md` (`.github/agent.env`) |
| `## Exit codes` (1119) | `reference/exit-codes.md` | `getting-started/lifecycle.md` (the contract in one line) |
| `## Security model` (1133) | `guides/security.md` | `guides/approvals.md`, `guides/api-policy.md` |
| `## Production checklist` (1186) | `guides/security.md` (task list, each item linked) | the guide each item points to |
| `## Disconnected profile` (1233) | `guides/offline.md` | `getting-started/lifecycle.md` (runs locally vs disconnected) |
| `## Compared with google-agents-cli` (1257) | `reference/comparison.md` | |
| `## Known limitations` (1297) | spread by topic (below); the parked issues are on `reference/known-issues.md` (included) | |
| `## Documentation` (1376) | `reference/index.md` ("Project resources") | `index.md` (next steps) |
| `## License` (1385) | site footer (`mkdocs.yml` copyright); `reference/index.md` | |

### Its "Known limitations" bullets

| Bullet | Page |
|---|---|
| LangGraph Server licence | `guides/develop.md` (runtimes) |
| A2A task store | `reference/http-api.md` (A2A) |
| Prompt injection | `guides/security.md` |
| No built-in inbound rate limiting | `guides/security.md` |
| Outbound `limits` are per process | `guides/api-policy.md` |
| Run lock across replicas | `reference/http-api.md` (one run per thread) |
| Rolling upgrades from an older build | `guides/upgrading.md` |
| A database that stops answering without closing its connections | `guides/deploy.md` |
| Concurrent deploys to one release | `guides/deploy.md` |
| `jwt` | `guides/authentication.md` |
| `langgraph-server` specifics | `guides/develop.md` (runtimes); `reference/http-api.md` |
| Human-in-the-loop | `guides/approvals.md` |
| `scaffold enhance` / `scaffold upgrade` rewrite the manifest | `guides/upgrading.md` |
| Upgrading a 0.1.0 project | `guides/upgrading.md` |
| The repository has no release tags yet | Dropped as stale: the tags v0.1.0 and v0.2.0 exist and the v0.2.0 release is published |
| A project made by a build between releases | `guides/upgrading.md` |
| The Bitnami subcharts come from `registry-1.docker.io` | `guides/deploy.md`; `guides/offline.md` |
| The generated workflows reference actions by version tag | `guides/cicd.md` |
