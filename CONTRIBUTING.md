# Contributing to graph-agents-cli

graph-agents-cli is a fork of [google-agents-cli](https://github.com/google/agents-cli)
(Apache-2.0; see [NOTICE](NOTICE)) that targets LangGraph agents on self-hosted
Kubernetes. Bug reports, feature requests, and pull requests are welcome through
the repository's issue tracker.

## Development setup

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Node.js (for `npx skills`), and, for the deployment paths, `helm`, `kubectl`, and a
Docker-compatible CLI. None of these is needed to run the test suite.

```bash
git clone https://github.com/ss7172/graph-agents-cli
cd graph-agents-cli
uv sync                              # creates .venv with the dev group
uv run graph-agents-cli --help       # run from the checkout
uv run graph-agents-cli setup --dev  # optional: editable `uv tool install` + skills from this checkout
```

`uv run` resolves against `uv.lock`; do not run `uv sync` in a shared working tree
unless you are adding a dependency, and add dependencies to `pyproject.toml` only.

## Tests

```bash
GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1 uv run pytest -q -m "not slow" -p no:cacheprovider   # fast suite (offline)
uv run pytest tests/setup -q                                     # one area
UV_NO_CONFIG=1 uv run pytest tests/template                      # slow: renders, uv sync, ruff, pytest, uv lock --locked, helm dependency build/lint/template
GRAPH_AGENTS_CLI_E2E=1 uv run pytest tests/integration/test_e2e.py -m slow   # slow: drives the CLI end to end through subprocesses
GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1 uv run graph-agents-cli login --status
```

Rules for tests:

- No fast test may need network, a Kubernetes cluster, or a model API key. Monkeypatch
  `graph_agents_cli._runner.run_resolved`, mock HTTP with `respx`, use the `fake`
  model provider, and exercise `--dry-run` paths. Tests that render and install a
  project or run helm are marked `slow` (registered in `pyproject.toml`) and are
  deselected by `-m "not slow"`.
- Rendered-project snapshots live under `tests/fixtures/rendered/<combo>/` and are
  regenerated with `uv run python scripts/regen_fixtures.py`. The script renders five
  combinations (`fastapi-none-memory`, `fastapi-k8s-postgres`,
  `fastapi-argocd-product-session`, `server-helm-push`, `fastapi-k8s-policy-process`)
  through the real `create -y --skip-checks --skip-deps --registry ghcr.io/e2e`
  (`--skip-deps` is a hidden `create` flag) and writes `files.json` (sorted relative
  file list) and `manifest.yaml` (`generated_at` replaced by `<generated_at>`) per
  combination; the sample policy input lives in
  `tests/fixtures/rendered/_inputs/product-policy.yaml`. Run it after a deliberate
  template change and review the diff of a regenerated fixture as carefully as the
  template change that caused it.
- `tests/integration/test_render_snapshots.py` (fast, offline) compares fresh renders to
  those fixtures; `tests/integration/test_help_parity.py` checks every command's
  `--help` and `short_help` (lazy commands: `short_help` equals the docstring's first
  line; the eagerly registered `secrets apply|status` and `infra check` only need
  `--help` to succeed); `tests/integration/test_e2e.py` is marked `slow` and runs only
  with `GRAPH_AGENTS_CLI_E2E=1` (it runs `uv sync` and `helm dependency build`, so it
  needs network, but never touches a cluster: `KUBECONFIG` is an empty file and every
  kubectl/helm-mutating path is `--dry-run`).
- `tests/skills/test_bundle.py` enforces the skills invariants (see *Skills*) and
  `tests/template` mirrors the engine's template layering to validate the rendered
  project.
- `tests/test_startup_imports.py` guards the lazy-import rule: nothing under
  `graph_agents_cli` may import a model SDK, LangChain, LangGraph, the Kubernetes
  client, or the Docker SDK at module import time. Shell out to `helm`, `kubectl`,
  `docker`, and `gh` through `graph_agents_cli._runner.run_resolved` /
  `graph_agents_cli._tools.require_tool` instead.

## Lint and format

```bash
uv run ruff check src tests --fix
uv run ruff format src tests
uv run python -m compileall -q src/graph_agents_cli -x 'scaffold/(agents|base_templates|deployment_targets)'
```

Ruff is configured in `pyproject.toml` (line length 100, `E,F,W,I,B,UP,RUF`). The
template directories under `src/graph_agents_cli/scaffold/` and `tests/fixtures` are
excluded because they contain Jinja placeholders (the template's Python files carry
Jinja in import statements so `--agent-directory` works), which is also why
`compileall` must skip them.

## How templates are rendered

`create` (and `scaffold enhance` / `scaffold upgrade`) render a project with
cookiecutter from three layers, merged in `scaffold/utils/template.py`:

1. `scaffold/base_templates/_shared` and `base_templates/python`: the manifest,
   `pyproject.toml`, `.env.example`, `.github/workflows`, guidance file.
2. `scaffold/agents/langgraph`: the agent code, `langgraph.json`, the two runtime
   Dockerfiles, `tests/eval`.
3. `scaffold/deployment_targets/kubernetes`: the Helm chart, values files, and the
   Argo CD `Application` manifests.

Runtime- and mode-specific files are selected with `CONDITIONAL_FILES`,
not with separate template layers: for example the `staging.yaml` and
`promote-to-prod.yaml` workflows are kept only when `cd != skip`, `deployment/argocd/**`
only when `cd == argocd`, and the Dockerfile is chosen by `runtime`. The variables a
template may use are defined by the scaffold configuration; update the configuration
and its tests when adding a variable.

The manifest written into every project (`graph-agents-cli-manifest.yaml`) records the
create parameters so `scaffold upgrade` can regenerate the exact prior baseline with the
prior CLI version (`uvx graph-agents-cli@<version>`) and 3-way merge it.

## How locks are regenerated

The LangGraph template ships two lock files, `uv-fastapi.lock` and
`uv-langgraph-server.lock`; `create` copies the one matching `--runtime` to `uv.lock`
and deletes the other. When you change the template's `pyproject.toml` regenerate both:

```bash
uv run python -m graph_agents_cli.scaffold.utils.generate_locks
```

The script (`src/graph_agents_cli/scaffold/utils/generate_locks.py`, runnable as
`__main__`) renders the template's `pyproject.toml` once per runtime with the project
name `locked-template` into a temporary directory that also holds an empty
`__init__.py` for every hatch wheel package (`app/`) and the readme when referenced,
runs `uv lock --no-config` there, replaces `locked-template` with
`{{cookiecutter.project_name}}`, and writes `uv-fastapi.lock` / `uv-langgraph-server.lock`
back into the template directory (`create` substitutes the placeholder when it copies
the chosen lock). It needs network access (or `UV_INDEX_URL` pointing at a mirror); the
fastapi lock is generated first so a partial run still leaves the default runtime
usable. It then tries `uv audit`; that subcommand does not exist in uv versions such as
the pinned 0.9.2, in which case it is skipped with a warning and the locks carry no
local advisory check. Commit the regenerated locks together with the `pyproject.toml`
change, refresh the snapshot fixtures, and run `UV_NO_CONFIG=1 uv run pytest tests/template`
(it asserts `uv sync --locked` and `uv lock --locked` on rendered projects).

## Skills

The six skills (`graph-agents-cli-workflow`, `-langgraph-code`, `-scaffold`, `-eval`,
`-deploy`, `-observability`) exist twice and must stay byte-identical:

- `skills/` at the repository root, read by `npx skills add <repo>` and the plugin
  manifests (`plugin.json`, `.claude-plugin/plugin.json`, `gemini-extension.json`);
- `src/graph_agents_cli/skills/data/`, bundled into the wheel so `setup` can install
  them without git or network (the second and third rungs of the install ladder).

Edit `skills/`, then sync the bundle and verify:

```bash
rm -rf src/graph_agents_cli/skills/data/graph-agents-cli-* \
  && cp -R skills/graph-agents-cli-* src/graph_agents_cli/skills/data/ \
  && cp skills/README.md src/graph_agents_cli/skills/data/README.md
diff -r skills src/graph_agents_cli/skills/data
```

`src/graph_agents_cli/skills/data` is excluded from ruff in `pyproject.toml`: `ruff format` also
formats Python code blocks inside Markdown, which would rewrite the bundled copy and break the
byte-identical invariant. Format `skills/` by hand if you want its code blocks reformatted.

```bash
uv run pytest tests/skills -q
```

`tests/skills/test_bundle.py` enforces: the two copies are byte-identical (ignoring
`.DS_Store`, `__pycache__`, `Thumbs.db`); exactly the six skills exist; every `SKILL.md`
has frontmatter with `name` equal to its directory, `metadata.version` `0.1.0`,
`metadata.license` `Apache-2.0` and `requires.bins: [graph-agents-cli]`, plus a
`## Not covered` and a `## Migration note` section; every `` `references/<file>.md` ``
a `SKILL.md` mentions exists and every reference file is mentioned (so cross-skill
pointers must not use that literal form); Google Cloud product names appear only under
a heading containing "Migration". Each `SKILL.md` carries `metadata.version`;
`_skills_check.py` compares it with the CLI version and asks the user to run
`graph-agents-cli update` when they differ, so bump it with each release. Keep the flag
lists in `skills/graph-agents-cli-workflow/references/commands.md` and
`skills/graph-agents-cli-scaffold/references/flags.md` equal to the real
`graph-agents-cli <command> --help` output.

## Compatibility and documentation

A change to a manifest key, environment variable, command flag, exit code, or chart
value must update the relevant README or skill reference, schemas where applicable,
and regression tests in the same pull request. Describe compatibility implications
and any required migration in the pull request.

Keep the public repository focused on reusable source, templates, tests and user
guidance. Do not add internal planning records, customer-specific design notes,
credentials or local generated projects.

## Headers and licensing

New files carry:

```
# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 ...
```

Files retained from google-agents-cli keep their `Copyright 2026 Google LLC` header
with `# Modifications Copyright 2026 graph-agents-cli contributors` added beneath it.
