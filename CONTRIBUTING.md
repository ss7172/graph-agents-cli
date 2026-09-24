# Contributing to graph-agents-cli

graph-agents-cli is a generic CLI for building, evaluating and deploying LangGraph agents on
self-hosted Kubernetes. It is a fork of [google-agents-cli](https://github.com/google/agents-cli)
(Apache-2.0; see [NOTICE](NOTICE)). Bug reports, feature requests and pull requests are
welcome through the repository's issue tracker.

Two rules shape every change:

- **Generic.** Nothing in the CLI, the template, the skills or a generated project may be
  shaped around one consumer. Consumers configure (auth policy, `api-policy.yaml`,
  environment variables, chart values); the CLI does not encode a consumer.
- **Fail closed.** A typo, a missing file or an unset variable must never widen access:
  refuse, and say what to fix.

## Development setup

Prerequisites: Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The fast test suite needs nothing else. The slow suites also need network access, `helm`
(the end-to-end and template tests render and lint charts) and `git`; Node.js is used by
`setup` for `npx skills`; the deployment paths use `kubectl` and a Docker-compatible CLI.

```bash
git clone https://github.com/ss7172/graph-agents-cli
cd graph-agents-cli
uv sync                              # .venv with the dev group, from uv.lock
uv run graph-agents-cli --help       # run from the checkout
uv run graph-agents-cli setup --dev  # optional: editable `uv tool install` + skills from this checkout
```

`uv run` resolves against `uv.lock`; add dependencies to `pyproject.toml` only, and commit
the lock with them. CI pins uv 0.9.2 (the version the bundled template locks are generated
with); use the same locally when you regenerate locks.

### Installing a build from a checkout

To use a development build as your `graph-agents-cli` outside `uv run` (to try it on a real
project, or to hand it to a reviewer), install it from the checkout:

```bash
uv tool install --from /path/to/graph-agents-cli graph-agents-cli   # a snapshot of the checkout
uv run graph-agents-cli setup --dev                                 # or: editable, follows the checkout
```

`pyproject.toml` (`[tool.uv] cache-keys`) keys uv's build cache on the checkout's commit, its
tags and every file under `src/`, so after a pull, a branch switch or an edit the same
command rebuilds and installs the new code; no `--reinstall` is needed. (uv's default key is
`pyproject.toml` alone, whose version does not change between releases: before these keys
it silently reinstalled a cached wheel of an earlier commit. A checkout at a commit that
predates them still needs `--reinstall`.) Use a separate `UV_TOOL_DIR` and
`UV_TOOL_BIN_DIR` to keep a development build beside a released one.

Builds between two releases share the version string; `graph-agents-cli --version` tells them
apart: `0.2.0` for a clean build of the commit the release tag `v0.2.0` names,
`0.2.0+g1a2b3c4` for any other commit, and `0.2.0+g1a2b3c4.dirty` when files under `src/`
(or `pyproject.toml`, `hatch_build.py`) had uncommitted changes. `graph-agents-cli info`
prints the full commit (`info --json`: `cli_build`). The facts come from
`graph_agents_cli/_build_info.json`, which `hatch_build.py` writes into every wheel and sdist
built from a git checkout (`src/graph_agents_cli/_build.py` computes them); an editable
install reads git when asked, and a tree without git builds a wheel identified by its
version only.

A project created by such a build records it in its manifest (`cli_build`: the id, the commit
and a digest of what it rendered), and `scaffold upgrade` rebuilds that commit as the old
snapshot, from the repository on GitHub. For a commit only your clone has, name it:
`graph-agents-cli scaffold upgrade --baseline-ref <clone>@<commit>`. A build with uncommitted
changes (`.dirty`) cannot be rebuilt at all, so commit before creating projects you mean to
upgrade later.

## Tests

| Tier | Command | Needs | Runs in CI |
|---|---|---|---|
| Fast | `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1 uv run pytest -q -m "not slow"` | nothing (no network, cluster or model key) | every pull request and push to `main` (Python 3.12 and 3.13) |
| End-to-end | `GRAPH_AGENTS_CLI_E2E=1 uv run pytest -q tests/integration` | network (uv sync, the bitnami subcharts), `helm` | nightly and on demand (`ci.yml`, job `e2e`) |
| Template (slow) | `UV_NO_CONFIG=1 uv run pytest -q -m slow tests/template` | network, `uv`, `helm` | nightly and on demand, with the end-to-end tier |

The CI job `e2e` runs both slow tiers at once:
`GRAPH_AGENTS_CLI_E2E=1 UV_NO_CONFIG=1 uv run pytest -q -m slow`. A plain `uv run pytest -q`
runs the fast suite plus the slow template tests (the end-to-end tests skip without
`GRAPH_AGENTS_CLI_E2E=1`).

- **Fast tests** must not need network, a Kubernetes cluster or a model API key.
  Monkeypatch `graph_agents_cli._runner.run_resolved`, mock HTTP with `respx`, use the
  `fake` model provider, and exercise `--dry-run` paths. A test that renders and installs a
  project or runs helm is marked `slow` (registered in `pyproject.toml`).
- **End-to-end tests** (`tests/integration/test_e2e.py`) drive the CLI through subprocesses:
  `create`, `install`, `run`, `eval`, `playground`, `lint`, `enhance`, `upgrade`, and the
  `deploy`/`secrets` dry runs. They never touch a cluster: `KUBECONFIG` is an empty file and
  every kubectl/helm-mutating path is `--dry-run`. Nothing may write to the developer's home:
  `tests/conftest.py` points the `enhance`/`upgrade` backup directory at a temporary one for
  every in-process test, and a subprocess that backs a project up gets its own `HOME`.
  `GRAPH_AGENTS_CLI_RUN_PORT` and `GRAPH_AGENTS_CLI_E2E_PLAYGROUND_PORT` (default 18790) move
  their ports so parallel runs do not collide. Anonymous pulls from `registry-1.docker.io`
  are rate-limited; a 429 from `helm dependency build` is an infrastructure failure, not a
  regression.
- **Template tests** (`tests/template`) render the template the way the engine does
  (`tests/template/render.py`), check the rendered files, workflows and charts, and in the
  slow tier install each rendered project and run its own ruff, tests, `uv lock --locked`,
  `helm lint` and `helm template`.
- **Docs parity** is part of the fast suite: `tests/integration/test_help_parity.py` loads
  every command and checks its `--help` and `short_help`; `tests/skills/test_bundle.py`
  checks the skills (see [Skills](#skills)); `tests/integration/test_render_snapshots.py`
  compares fresh renders with the committed fixtures.
- `tests/test_startup_imports.py` guards the lazy-import rule: nothing under
  `graph_agents_cli` may import a model SDK, LangChain, LangGraph, the Kubernetes client or
  the Docker SDK at import time. Shell out to `helm`, `kubectl`, `docker` and `gh` through
  `graph_agents_cli._runner.run_resolved` / `graph_agents_cli._tools.require_tool`.

For a template change, also scaffold a project into a scratch directory with the dev CLI and
run its own checks with the fake model:

```bash
uv run graph-agents-cli create try -o /tmp/scratch -y --skip-checks
cd /tmp/scratch/try && uv sync --locked
MODEL_PROVIDER=fake API_KEY=dev uv run pytest tests/unit tests/integration
uv run --project <checkout> graph-agents-cli lint
```

### Rendered-project fixtures

Snapshots of rendered projects live under `tests/fixtures/rendered/<combo>/`:
`files.json` (the sorted file list) and `manifest.yaml` (the rendered manifest with
`generated_at` replaced by `<generated_at>` and without the `cli_build` block, which changes
with every commit; `test_render_snapshots.py` checks that block on its own: this build's id,
and a digest equal to a fresh render from the manifest's settings). `scripts/regen_fixtures.py` renders six
combinations (`fastapi-none-memory`, `fastapi-k8s-postgres`, `fastapi-argocd-custom`,
`server-helm-push`, `fastapi-k8s-policy-process`, `fastapi-none-jwt`) through the real
`create -y --skip-checks --skip-deps --registry ghcr.io/e2e` (`--skip-deps` is a hidden
flag); the sample policy input is `tests/fixtures/rendered/_inputs/api-policy.yaml`.

```bash
uv run python scripts/regen_fixtures.py
git diff tests/fixtures/rendered
```

Run it after a deliberate template change or a version bump (the manifest records
`cli_version`), and review the fixture diff as carefully as the change that caused it.

## Lint and format

```bash
uv run ruff check . --fix
uv run ruff format .
uv run python -m compileall -q src/graph_agents_cli -x 'scaffold/(agents|base_templates|deployment_targets)'
```

Ruff is configured in `pyproject.toml` (line length 100, `E,F,W,I,B,UP,RUF`). The template
directories under `src/graph_agents_cli/scaffold/`, the bundled skills and `tests/fixtures`
are excluded: the templates carry Jinja placeholders (in import statements too, so
`--agent-directory` works), which is also why `compileall` must skip them.

Check GitHub workflows with [actionlint](https://github.com/rhysd/actionlint) (the image
includes shellcheck): this repository's in place, the template's in a rendered project
(they are Jinja-free but only complete once rendered). Without file arguments actionlint
looks for a git repository, so name the workflows in a freshly created project that is not
one yet:

```bash
# this repository (a git checkout)
docker run --rm -v "$PWD:/repo" --workdir /repo rhysd/actionlint:latest -no-color
# a rendered project (git or not)
docker run --rm -v "$PWD:/repo" --workdir /repo rhysd/actionlint:latest -no-color \
  .github/workflows/*.yaml
```

## How templates are rendered

`create` (and `scaffold enhance` / `scaffold upgrade`) render a project with cookiecutter from
three layers, merged in `scaffold/utils/template.py` in this order (a later layer overwrites
the files of an earlier one, so the agent overlay wins):

1. `scaffold/base_templates/_shared` and `base_templates/python`: the manifest, the
   guidance file, `.github/` (workflows, `agent.env`, `CODEOWNERS`).
2. `scaffold/deployment_targets/kubernetes`: the Helm chart, the values files, the Argo CD
   `Application` manifests and the CD workflows.
3. `scaffold/agents/langgraph` (the agent overlay): the agent code, `langgraph.json`, the two
   runtime Dockerfiles, `.env.example`, `api-policy.yaml` (a sample: `create --api-policy`
   replaces it with the seed file, and a project without a policy gets none), the tests.

Runtime- and mode-specific files are selected with `CONDITIONAL_FILES`, not separate layers:
for example `staging.yaml` and `promote-to-prod.yaml` are kept only when `cd != skip`,
`deployment/argocd/**` only when `cd == argocd`, `api-policy.yaml` and
`tools/example_api.py` only with a policy, and the Dockerfile is chosen by `runtime`. The
variables a template may use are defined by the scaffold configuration
(`build_cookiecutter_context`); update it and its tests when adding one.

The manifest written into every project (`graph-agents-cli-manifest.yaml`) records the
create parameters and `cli_version`, so `scaffold upgrade` can regenerate the exact prior
baseline with the prior CLI version (`uvx --from <install spec of that version>
graph-agents-cli`, see `scaffold/utils/version.py`) and 3-way merge it. Upgrade never
rewrites agent code (`agent.py`, `tools/**`, `policies/**`) or config (`.env*`,
`api-policy.yaml`, `values-*.yaml`, `deployment/argocd/**`, eval datasets and config): a
change there needs a migration note in CHANGELOG.md.

### The shared API-policy rule block

The `api-policy.yaml` schema and call-matching rules exist twice: in
`src/graph_agents_cli/_api_policy.py` (used by `create --api-policy` and `lint`) and in the
template's `app/app_utils/api_client.py` (the runtime). The code between the
`--- BEGIN SHARED API POLICY RULES ---` and `--- END SHARED API POLICY RULES ---` markers must
stay byte-identical, so `create`, `lint` and the running agent accept the same files, report
the same errors and refuse the same calls. `tests/dev/test_api_policy_parity.py` enforces
the byte identity and feeds the same valid and invalid policies and calls to both copies.

To change a rule: edit the CLI copy, copy the whole block into the template, and add cases to
the parity test. Keep the block free of Jinja and of imports the template does not have, and
fail closed: an ambiguous or unknown input is an error, never an allow. The block holds the
schema (including `limits` and the `approval` block) and the matching rules, `gated()`
included (which allowed calls wait for whose approval); stateful enforcement (the per-run call
counts and the per-process rate buckets of `limits`, pausing a gated call and binding its
approval) lives outside it, in the runtime only.

### `graph-agents-cli api` and comment-preserving edits

`api-policy.yaml` belongs to the project and changes over the agent's life through
`graph-agents-cli api` (`src/graph_agents_cli/api/`): each command validates the current file
with the shared rules, applies one change, validates the result, and prints a diff of every
file it touches (the policy, the manifest, `.env.example`, the chart's `values.yaml`) before
writing them atomically. The edits go through `scaffold/utils/keyedit.py`, which changes the
text at the positions PyYAML reports (built on the key merge of `keymerge.py`) and re-parses
the result, which must equal the old document plus exactly that change; anything it cannot
edit safely is an error or a "Left for you" item, never a reformatted file. Keep `create`
seeding the file only (`--api-policy`), never inventing access: there is no default access
level anywhere, and samples, examples and messages must not suggest one.

## How locks are regenerated

The LangGraph template ships two lock files, `uv-fastapi.lock` and `uv-langgraph-server.lock`;
`create` copies the one matching `--runtime` to `uv.lock` and deletes the other. When you
change the template's `pyproject.toml`, regenerate both:

```bash
uv run python -m graph_agents_cli.scaffold.utils.generate_locks
```

The script renders the template's `pyproject.toml` once per runtime (project name
`locked-template`) into a temporary directory, runs `uv lock --no-config` there, replaces
the name with `{{cookiecutter.project_name}}`, and writes both locks back into the template
(`create` substitutes the placeholder). It needs network (or `UV_INDEX_URL` pointing at a
mirror). It re-resolves from scratch, so minor upstream versions move too: review the lock
diff. The server image's base tag (`Dockerfile.langgraph-server`) must match the
`langgraph-api` version in `uv-langgraph-server.lock`; the image build refuses a mismatch.
Commit the locks with the `pyproject.toml` change, refresh the fixtures, and run the slow
template tests.

## Skills

The six skills (`graph-agents-cli-workflow`, `-langgraph-code`, `-scaffold`, `-eval`,
`-deploy`, `-observability`) exist twice and must stay byte-identical:

- `skills/` at the repository root, read by `npx skills add <repo>` and the plugin manifests
  (`plugin.json`, `.claude-plugin/plugin.json`, `gemini-extension.json`);
- `src/graph_agents_cli/skills/data/`, bundled into the wheel so `setup` can install them
  without git or network.

Edit `skills/`, then sync the bundle and verify:

```bash
rm -rf src/graph_agents_cli/skills/data/graph-agents-cli-* \
  && cp -R skills/graph-agents-cli-* src/graph_agents_cli/skills/data/ \
  && cp skills/README.md src/graph_agents_cli/skills/data/README.md
diff -r -x .DS_Store skills src/graph_agents_cli/skills/data
uv run pytest tests/skills -q
```

`ruff format` also formats the Python blocks inside Markdown files (the README, `skills/`), and
CI runs `ruff format --check .`. `src/graph_agents_cli/skills/data` is excluded from ruff so the
bundle is never reformatted on its own: run `ruff format` first, then sync the bundle.

`tests/skills/test_bundle.py` enforces: the two copies are byte-identical (ignoring
`.DS_Store`, `__pycache__`, `Thumbs.db`); exactly the six skills exist; every `SKILL.md`
has frontmatter with `name` equal to its directory, `metadata.version` equal to the release
(`EXPECTED_VERSION`), `metadata.license` `Apache-2.0` and `requires.bins:
[graph-agents-cli]`, plus a `## Not covered` and a `## Migration note` section; every
`` `references/<file>.md` `` a `SKILL.md` mentions exists and every reference file is
mentioned; Google Cloud product names appear only under a heading containing "Migration".
`_skills_check.py` compares the installed skills' `metadata.version` with the CLI version and
asks the user to run `graph-agents-cli setup` (the skills of this version) or `update` when they
differ. `setup` installs the skills from this repository at the tag of the running release
(`<repo>#v<version>`; the default branch only for a development build), so the release tag
must exist before users install that version. Keep
`skills/graph-agents-cli-workflow/references/commands.md` and
`skills/graph-agents-cli-scaffold/references/flags.md` equal to the real `--help` output, and
never document hidden or deprecated flags there.

## Compatibility and documentation

A change to a manifest key, environment variable, command flag, exit code, endpoint or chart
value updates, in the same pull request: the README, the template's `README.md`,
`.env.example` and guidance file when a generated project is affected, the relevant skill
references (both copies), schemas where applicable, regression tests, and CHANGELOG.md
(`## [Unreleased]`, with migration steps for anything breaking). Describe compatibility
implications in the pull request.

Keep the public repository focused on reusable source, templates, tests and user guidance.
Shipped files explain their rationale in place; they never cite internal planning records,
and the repository holds no customer-specific design notes, credentials or generated
projects (`docs/` is ignored for local notes).

## Release process

Versions follow [Semantic Versioning](https://semver.org/); while the version is 0.x a minor
release may break compatibility, with migration steps in CHANGELOG.md. Only the repository
owner tags releases.

1. **Bump the version** everywhere it lives: `pyproject.toml` (`version`), `uv.lock` (run
   `uv lock`), `plugin.json`, `.claude-plugin/plugin.json`, `gemini-extension.json`,
   `metadata.version` in every `SKILL.md` (both copies; `sed -i` or an editor, then the
   bundle sync above), the workflow skill's `Requires: graph-agents-cli ~= X.Y.Z` line,
   `EXPECTED_VERSION` in `tests/skills/test_bundle.py`, the `cli_version` example in the
   langgraph-code skill's `template-contract.md`, and every pinned install command
   (`git grep -n '@v<old version>' -- README.md skills` finds the `uv tool install ...@vX.Y.Z`
   lines, including each `SKILL.md`'s `requires.install`).
2. **Regenerate the fixtures** (`uv run python scripts/regen_fixtures.py`): the manifests
   record `cli_version`. Generated projects pin `GRAPH_AGENTS_CLI_SPEC` to
   `git+https://github.com/ss7172/graph-agents-cli@vX.Y.Z` from the installed version.
3. **CHANGELOG.md**: move `## [Unreleased]` into `## [X.Y.Z] - <date>` and update the links
   at the bottom. The release workflow refuses a tag without that section and uses it as the
   release notes.
4. **Checks**: fast suite, `ruff check .`, `ruff format --check .`, and the slow tiers
   (`GRAPH_AGENTS_CLI_E2E=1 UV_NO_CONFIG=1 uv run pytest -q -m slow`, or a manual run of the
   `ci` workflow's `e2e` job on the release commit).
5. **Tag every earlier release that projects may upgrade from.** `scaffold upgrade` rebuilds
   a project's baseline from `git+https://github.com/ss7172/graph-agents-cli@v<cli_version>`,
   so each version a project can record needs its tag on the remote. 0.1.0 was never tagged:
   before (or together with) the first `v0.2.x` tag, push it once at commit `fc3f2f9`:
   `git tag -a v0.1.0 fc3f2f9 -m "graph-agents-cli 0.1.0" && git push origin v0.1.0`. That
   commit has no workflows, so the tag creates no GitHub Release. Check it with a project
   created by 0.1.0: `graph-agents-cli scaffold upgrade --dry-run` lists about 30 files under
   "Will auto-update" (without the tag it stops with exit 2). Tags are the owner's action
   alone; until one exists on the remote, users name that baseline with `scaffold upgrade
   --baseline-ref <clone>@<commit>`. Projects also record the build that rendered them
   (`cli_build` in the manifest: build id, commit, digest of the render); one made by a
   build between releases is upgraded from that commit on the remote, so push the branch
   such builds come from, or those users need `--baseline-ref` too.
6. **Tag and push**: `git tag -a vX.Y.Z -m "graph-agents-cli X.Y.Z" && git push origin vX.Y.Z`.
7. `.github/workflows/release.yml` then checks that the tag equals the package version (and
   the plugin manifests, the skills' `metadata.version` and the changelog agree), runs ruff
   and the fast suite, builds the sdist and wheel with `uv build`, checks that the wheel
   installs and reports the version (exactly `version X.Y.Z`: `hatch_build.py` marks the
   build a release only when the tag `vX.Y.Z` names the checked-out commit and the tree is
   clean; any other build reports `X.Y.Z+g<commit>`), and creates the GitHub Release with
   the sdist, the wheel, `SHA256SUMS` and the changelog section as notes (a version with
   `a`, `b`, `rc` or `dev` is a prerelease).
8. **PyPI (off by default).** The `pypi` job runs only when the variable `PUBLISH_TO_PYPI`
   is `true`. Before turning it on, the owner registers the project on PyPI with a trusted
   publisher (owner `ss7172`, repository `graph-agents-cli`, workflow `release.yml`,
   environment `pypi`) and creates the `pypi` environment in the repository settings with
   required reviewers. That environment and the trusted-publisher registration are the real
   gate: the job's `if:` is a GitHub expression, which compares strings case-insensitively
   (`TRUE` passes it) and reads organization variables as well as repository ones (a
   repository variable wins over an organization variable of the same name). The job's
   first step therefore refuses any value but exactly `true`. Publishing uses OIDC, no
   token. Once a version is on PyPI, `install_spec()` in `scaffold/utils/version.py` is the
   one place to switch the default install spec to the index, and the README install
   section changes with it.

The workflows pin actions to full commit SHAs with the version in a comment. To bump one,
resolve the new tag's commit (`git ls-remote https://github.com/<owner>/<action>
refs/tags/<tag>`, and the `^{}` entry for an annotated tag), update the SHA and the comment
together, and run actionlint.

## Upstream sync

graph-agents-cli is based on google-agents-cli 1.6.1. About half of the code (the scaffold
engine, remote templates, the 3-way merge, the extension system, parts of `setup`) is
inherited, so upstream fixes to those parts matter here. Review each upstream release:

1. Read the release notes (`RELEASE_NOTES.md` in google/agents-cli) and the diff between
   the upstream tags (`git fetch https://github.com/google/agents-cli --tags`, then
   `git diff <old>..<new> -- src/`).
2. Classify each change: **port** (a fix or security hardening in an inherited subsystem:
   template rendering, remote template fetching, merge, extensions, skills installation,
   CLI plumbing), **adapt** (an idea that applies to LangGraph or Kubernetes but needs its
   own implementation, for example a new eval capability), or **skip** (Google Cloud, ADK,
   Gemini Enterprise, Terraform, other languages).
3. Port with a regression test, keeping the Google LLC header plus the "Modifications
   Copyright" line on retained files. Credit the upstream version in the commit message and
   add a CHANGELOG entry.
4. Open an issue for each **adapt** item and record skipped items in the pull request, so
   the next review starts where this one ended.

Known pending item: upstream 1.7.0 changed how remote templates handle symlinks; this fork
still skips every symlink in a fetched template.

## Headers and licensing

New files carry:

```
# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 ...
```

Files retained from google-agents-cli keep their `Copyright 2026 Google LLC` header with
`# Modifications Copyright 2026 graph-agents-cli contributors` added beneath it.
