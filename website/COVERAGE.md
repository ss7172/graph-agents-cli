# Documentation coverage map

This file is for the people writing the site; it is outside `docs_dir` (`website/src/`) and is
never built or published. It maps every section of the pre-site `README.md` to the page(s)
that must carry its facts, assigns every page to one writing lane, and sets the rules the
lanes share. Every README fact must land on at least one page; when the README and the code
disagree, the code wins and the writer notes the difference in their report.

README line numbers refer to `README.md` at commit 7ece9d2 (v0.2.0), the README the site
replaces.

## Lanes and pages

A page belongs to exactly one lane; only that lane edits it. `done` pages are finished by the
scaffold and only change through the scaffold owner.

| Page (`website/src/…`) | Nav title | Lane | Status |
|---|---|---|---|
| `index.md` | Home | scaffold | done |
| `getting-started/index.md` | Get started | scaffold | done |
| `getting-started/installation.md` | Installation & setup | getstarted | done |
| `getting-started/quickstart.md` | Quickstart | getstarted | done |
| `getting-started/tutorial-coding-agent.md` | Tutorial: build with a coding agent | getstarted | done |
| `getting-started/tutorial-manual.md` | Tutorial: manual workflow | getstarted | done |
| `getting-started/lifecycle.md` | The lifecycle | getstarted | done |
| `guides/index.md` | Guides | scaffold | done |
| `guides/develop.md` | Develop your agent | guides_build | done |
| `guides/authentication.md` | Authentication | guides_build | done |
| `guides/api-policy.md` | Outbound API policy | guides_build | done |
| `guides/approvals.md` | Human approval | guides_build | done |
| `guides/evaluation.md` | Evaluation | guides_build | done |
| `guides/extensions.md` | Extensions | guides_build | done |
| `guides/deploy.md` | Deploy to Kubernetes | guides_ops | done |
| `guides/cicd.md` | CI/CD | guides_ops | done |
| `guides/secrets.md` | Secrets | guides_ops | done |
| `guides/observability.md` | Observability | guides_ops | done |
| `guides/upgrading.md` | Upgrading projects | guides_ops | done |
| `guides/offline.md` | Offline profile | guides_ops | done |
| `guides/security.md` | Security & production | guides_ops | done |
| `reference/index.md` | Reference | scaffold | done |
| `reference/cli.md` | CLI | scaffold | done (generated: mkdocs-click + `hooks/cli_reference.py`) |
| `reference/environment.md` | Environment variables | reference | done |
| `reference/http-api.md` | HTTP API | reference | done |
| `reference/api-policy-schema.md` | api-policy.yaml | reference | done |
| `reference/manifest.md` | Project manifest | reference | done |
| `reference/exit-codes.md` | Exit codes | reference | done |
| `reference/skills.md` | Skills | scaffold | done (generated: `hooks/skills_reference.py`) |
| `reference/comparison.md` | Compared with google-agents-cli | reference | done |
| `reference/known-issues.md` | Known issues | scaffold | done (includes `KNOWN_ISSUES.md`) |
| `reference/changelog.md` | Changelog | scaffold | done (includes `CHANGELOG.md`) |

Each stub holds its brief in an HTML comment: the facts it must cover, their sources (README
sections, CHANGELOG entries, KNOWN_ISSUES ids, skills files, code modules), what to verify by
running, and the pages it must link to.

## README section map

Every `##` and `###` heading of the README, with the page(s) that carry it. "Primary" pages
hold the full facts; "also" pages summarize and link to the primary.

| README heading (line) | Primary page(s) | Also | Lane |
|---|---|---|---|
| `# graph-agents-cli` intro (1-31) | `index.md` (pitch, generic, skills); `reference/comparison.md` (fork, NOTICE) | `getting-started/lifecycle.md` | scaffold (done) / reference (done) |
| `## Install` (33) | `getting-started/installation.md` | `reference/environment.md` (GRAPH_AGENTS_CLI_INSTALL_SPEC, NO_UPDATE_CHECK); `guides/upgrading.md` (install spec `{version}`); `guides/offline.md` (mirror, wheel skills) | getstarted (done) |
| `## Quick start` (83) | `getting-started/quickstart.md` | `guides/authentication.md` (jwt dev-token block); `getting-started/tutorial-manual.md` (create options); `guides/deploy.md` (local cluster, registry placeholder paragraph) | getstarted (done) |
| `## Commands` (145) | `reference/cli.md` (generated: every command and flag) | `getting-started/lifecycle.md` (commands by stage); `reference/environment.md` (the "CLI environment variables" paragraph); each guide for its commands | scaffold (done) / getstarted (done) / reference (done) |
| `### Upgrading a project` (196) | `guides/upgrading.md` | `reference/manifest.md` (cli_version, cli_build, template_digest) | guides_ops (done) |
| `## The generated service` (240) | `guides/develop.md` (tree, runtimes) | `reference/http-api.md` | guides_build (done) |
| `### Endpoints` (267): route table | `reference/http-api.md` | `guides/develop.md` (summary table) | reference (done) |
| `### Endpoints` bullets One run per thread, Limits, Thread ids, Timeouts, Step limit, A valid history, Tool arguments, Errors, Run records | `reference/http-api.md` | `guides/security.md` (Thread ids); `guides/observability.md` (Run records) | reference (done) |
| `### Endpoints` bullets Retention, Logging, Tracing | `guides/observability.md` | `reference/environment.md` | guides_ops (done) |
| `### Endpoints` bullet Database | `guides/deploy.md` (External database section) | `reference/environment.md` (DB_POOL_*) | guides_ops (done) |
| `### Endpoints` bullets Settings from `.env`, CORS, closing paragraph (defaults, startup parse rules) | `reference/environment.md` | `guides/develop.md` | reference (done) |
| `## Authentication` (381) | `guides/authentication.md` | `reference/environment.md` (AUTH_* names) | guides_build (done) |
| `` ### `shared-bearer` (default) `` (400) | `guides/authentication.md` | `guides/secrets.md` (API_KEY generation) | guides_build (done) |
| `` ### `jwt` `` (408) | `guides/authentication.md` (full AUTH_JWT_* table) | `reference/environment.md` (names, link) | guides_build (done) |
| `` ### `custom` `` (442) | `guides/authentication.md` | `reference/manifest.md` (auth_policy_implemented) | guides_build (done) |
| `` ## Outbound API policy (`api-policy.yaml`) `` (486) | `guides/api-policy.md` (concepts, access, auth modes, per-user authorization, limits, API_CALLS, lint) | `reference/api-policy-schema.md` (every key, the fail-closed matching rules) | guides_build (done) |
| `` ### Human approval of calls (`approval`) `` (613) | `guides/approvals.md` | `reference/api-policy-schema.md` (approval schema); `reference/http-api.md` (approval routes, A2A); `guides/evaluation.md` (eval approvals) | guides_build (done) |
| `### The policy's lifecycle` (785) | `guides/api-policy.md` | `getting-started/tutorial-manual.md` (worked example) | guides_build (done) |
| `## Evaluation` (843) | `guides/evaluation.md` | `getting-started/quickstart.md` (one paragraph) | guides_build (done) |
| `## Environments and CD modes` (897): environments, rules, deploy flags, local clusters | `guides/deploy.md` | `getting-started/lifecycle.md` (at a glance) | guides_ops (done) |
| `## Environments and CD modes` (897): CD mode table, CI column, GH_HOST | `guides/cicd.md` | `guides/deploy.md` (deploy column) | guides_ops (done) |
| `### Chart` (982) | `guides/deploy.md` | `guides/observability.md` (metrics values); `guides/security.md` (pod security, NetworkPolicy) | guides_ops (done) |
| `### External database` (1025) | `guides/deploy.md` | `guides/security.md` (checklist item) | guides_ops (done) |
| `## Secrets` (1049) | `guides/secrets.md` | `guides/cicd.md` (who runs `secrets apply` in argocd / helm-push) | guides_ops (done) |
| `` ### Required GitHub settings (`helm-push`, `argocd`) `` (1084) | `guides/cicd.md` | `reference/manifest.md` (`.github/agent.env`) | guides_ops (done) |
| `## Exit codes` (1119) | `reference/exit-codes.md` | `getting-started/lifecycle.md` (the contract in one line) | reference (done) |
| `## Security model` (1133) | `guides/security.md` | `guides/approvals.md`, `guides/api-policy.md` | guides_ops (done) |
| `## Production checklist` (1186) | `guides/security.md` (task list, each item linked) | the guide each item points to | guides_ops (done) |
| `## Disconnected profile` (1233) | `guides/offline.md` | `getting-started/lifecycle.md` (runs locally vs disconnected) | guides_ops (done) |
| `## Compared with google-agents-cli` (1257) | `reference/comparison.md` | | reference (done) |
| `## Known limitations` (1297) | spread by topic (below); the parked issues are on `reference/known-issues.md` (included) | | per row below |
| `## Documentation` (1376) | `reference/index.md` ("Project resources") | `index.md` (next steps) | scaffold (done) |
| `## License` (1385) | site footer (`mkdocs.yml` copyright); `reference/index.md` | | scaffold (done) |

### `## Known limitations` bullets

| Bullet | Page | Lane |
|---|---|---|
| LangGraph Server licence | `guides/develop.md` (runtimes) | guides_build (done) |
| A2A task store | `reference/http-api.md` (A2A) | reference (done) |
| Prompt injection | `guides/security.md` | guides_ops (done) |
| No built-in inbound rate limiting | `guides/security.md` | guides_ops (done) |
| Outbound `limits` are per process | `guides/api-policy.md` | guides_build (done) |
| Run lock across replicas | `reference/http-api.md` (one run per thread) | reference (done) |
| Rolling upgrades from an older build | `guides/upgrading.md` | guides_ops (done) |
| A database that stops answering without closing its connections | `guides/deploy.md` | guides_ops (done) |
| Concurrent deploys to one release | `guides/deploy.md` | guides_ops (done) |
| `jwt` | `guides/authentication.md` | guides_build (done) |
| `langgraph-server` specifics | `guides/develop.md` (runtimes); `reference/http-api.md` | guides_build (done) / reference (done) |
| Human-in-the-loop | `guides/approvals.md` | guides_build (done) |
| `scaffold enhance` / `scaffold upgrade` rewrite the manifest | `guides/upgrading.md` | guides_ops (done) |
| Upgrading a 0.1.0 project | `guides/upgrading.md` | guides_ops (done) |
| The repository has no release tags yet | **stale, drop it**: tags v0.1.0 and v0.2.0 exist and the v0.2.0 release is published (checked 2026-09-24) | getstarted (done: not repeated on `installation.md`) |
| A project made by a build between releases | `guides/upgrading.md` | guides_ops (done) |
| The Bitnami subcharts come from `registry-1.docker.io` | `guides/deploy.md`; `guides/offline.md` | guides_ops (done) |
| The generated workflows reference actions by version tag | `guides/cicd.md` | guides_ops (done) |

## Rules for every lane

- **Only your pages.** Edit only the pages of your lane. Shared files belong to the scaffold
  owner: `website/mkdocs.yml`, `website/hooks/`, `website/overrides/`,
  `website/src/stylesheets/`, `website/src/assets/`, the `index.md` pages, and the four `done`
  reference pages. Need a change there (a new Markdown extension, a component, a hook)? Ask.
- **Facts come from the code.** Run the commands (`--help`, and the real flow in a scratch
  directory) with `MODEL_PROVIDER=fake` and `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`; paste real
  output. Start local servers only on ports in your assigned range (`run` honours
  `GRAPH_AGENTS_CLI_RUN_PORT`, `playground` takes `--port`) and stop them afterwards.
- **Public sources only.** Never read or cite the repository's `docs/` directory (private and
  git ignored). Upstream pages: `git show HEAD:<path>` in a google/agents-cli checkout (tracked files only; your task names its location).
- **Generic.** No consumer names, no domain-specific examples beyond the orders / payments
  examples the README already uses. No exploit-level detail: describe risks and controls.
- **Shape.** One idea per section, short paragraphs, tables for reference data, admonitions
  (`!!! warning`) for warnings, content tabs (`=== "OpenAI"`) for alternatives, a grid of
  cards for "next steps". Keep the H1 and the front-matter `description`.
- **Links.** Relative links to `.md` files (`../reference/cli.md#graph-agents-cli-deploy`).
  `mkdocs build --strict` fails on a missing page or anchor; CLI anchors are
  `#graph-agents-cli-<command>[-<subcommand>]`. Link a command's flags to the CLI reference
  instead of copying the flag list.
- **Install tags.** Write install commands with the current release tag
  (`git+https://github.com/ss7172/graph-agents-cli@v0.2.0`); `hooks/version.py` fails the build
  when a page names another version, so release bumps cannot leave stale commands behind.
- **Check before you hand in:** `uv run --group docs mkdocs build --strict -f website/mkdocs.yml`
  passes, and `uv run --group docs mkdocs serve -f website/mkdocs.yml -a 127.0.0.1:<port>`
  (a port in your range) looks right at desktop and phone widths.

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

## Findings for the owner (from the scaffold, 2026-09-24)

- `KNOWN_ISSUES.md` line 6 links to `README.md#known-limitations`; that section leaves the
  README when it is slimmed. Point the link at the site (or at the page that keeps the list)
  in the same change. The site rewrites the file's repository-relative links at build time
  (`hooks/repo_links.py`), so the page keeps working either way.
- `CHANGELOG.md` and `KNOWN_ISSUES.md` contain a few `<placeholder>` words outside code spans
  (for example `<error_id>`, `<clone>@<commit>`). GitHub and the site both drop them as unknown
  HTML tags; wrapping them in backticks fixes both.
- `graph-agents-cli update`'s short help says it reinstalls the skills, but it also upgrades the
  CLI to the latest release (its docstring says so): the site describes the behaviour.
- mkdocs-click titles commands by their Click name, so `info` (the function `cmd_info`) would
  read `cmd-info`; `hooks/cli_reference.py` renders it as `info`, and its build-time guard
  fails the build if any command or subcommand is missing from the reference.
- Tests that execute README examples must move to the site when the README is slimmed (a
  separate change): `tests/api/test_api_cmd.py` runs the `graph-agents-cli api` lines of the
  first ```` ```bash ```` block after the text "Adding functionality to a working agent"
  (`_readme_example`) and of every ```` ```bash ```` block in the "Human approval of calls"
  section (`_readme_approval_examples`, expecting blocks of 2, 1 and 1 `api approval`
  commands). The guides_build lane keeps those examples in that shape in
  `guides/api-policy.md` and `guides/approvals.md` so the tests can be re-targeted unchanged.
