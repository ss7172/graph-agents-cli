# Command flag reference

Run `graph-agents-cli <command> --help` for the authoritative list.

## `graph-agents-cli create <name>` (alias: `scaffold create`)

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--agent` | `-a` | `langgraph` | Agent template: `langgraph` (bundled), `local@<path>`, or `<org>/<repo>/<path>@<ref>` / a git URL for a remote template |
| `--runtime` | | `fastapi` | `fastapi` (uvicorn on `app.fast_api_app:app`) or `langgraph-server` (`langgraph-api` image; Postgres + Redis) |
| `--model-provider` | | `openai` (prompted with `-i`) | `openai`, `anthropic`, `gemini`, `openai-compatible` |
| `--model` | | provider default | Model name written to `.env` (`MODEL_NAME`) and the manifest |
| `--checkpointer` | | `postgres` for kubernetes, `memory` for `none` | Deployed default; validated against the combination table |
| `--deployment-target` | `-d` | `kubernetes` (`none` under `--prototype`) | `kubernetes` renders the Helm chart, environments, and workflows; `none` renders code only |
| `--registry` | | `ghcr.io/<org>` | Image registry and org; `<org>` from the git `origin` owner under `-y`, else `ghcr.io/CHANGE-ME` with a warning |
| `--cd` | | `skip` (forced under `--prototype`) | `argocd` (pull-based, Argo `Application`s, PR flow), `helm-push` (self-hosted runner runs `deploy --image`), `skip` (CI only) |
| `--auth-policy` | | `shared-bearer` | `shared-bearer` (API key), `jwt` (per-user OIDC/JWT tokens) or `custom` (stub; sets `auth_policy_implemented: false`); `product-session` is refused with a hint to `custom` |
| `--api-policy` | | none | Path to an `api-policy.yaml` to seed at the project root (validated with the strict schema first, exit 3 on errors); copies the OpenAPI specs it references; adds `api_policy.policy_file` to the manifest. Optional: without it the project has no policy until `graph-agents-cli api add`. `--product-policy` is refused with a rename hint |
| `--process` | | none | Path to the governing process document; written as `process:` to the manifest and rendered into the guidance file |
| `--prototype` | `-p` | off | Target defaults to `none` unless given explicitly; `--cd` forced to `skip` |
| `--agent-directory` | `-dir` | `app` | Agent code directory inside the project |
| `--agent-guidance-filename` | | `AGENTS.md` | `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md` |
| `--output-dir` | `-o` | `.` | Parent directory for the new project |
| `--base-template` | `-bt` | template default | Base template underneath a remote `--agent` template (remote templates only) |
| `--skip-checks` | `-s` | off | Skip the preflight check (only `uv` on PATH; the git-origin registry lookup still runs) |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive: defaults for anything not given |
| `--interactive` | `-i` | off | Menus and prompts for a human at a terminal |
| `--debug` | | off | Debug logging |

Project names must match `^[A-Za-z0-9][A-Za-z0-9_-]*$` (26 characters at most) and are
normalised to lowercase with hyphens (`My_Agent` -> `my-agent`), because the name becomes the Helm
release and the namespace prefix.

Validation (`create` refuses otherwise):

- runtime x checkpointer x target must be a valid row of the table in `SKILL.md`
  (`memory` + `kubernetes` is refused for both runtimes);
- `--cd argocd|helm-push` requires `--deployment-target kubernetes`;
- `--deployment-target none` defaults `--checkpointer memory`;
- an `auth: forward` API in `--api-policy` is refused under `--runtime langgraph-server`;
- `GRAPH_AGENTS_CLI_INSTALL_SPEC`, when set, must be one install spec without control characters or
  whitespace (exit 3 before anything is rendered).

What each choice renders:

| Choice | Files |
|---|---|
| (always) | `.github/workflows/pr_checks.yaml`, `.github/agent.env` (`GRAPH_AGENTS_CLI_SPEC`), `.github/CODEOWNERS`, `.env.example`, the guidance file |
| `--deployment-target kubernetes` | `deployment/helm/<name>/**`, `environments:` in the manifest, chart settings in `.github/agent.env`, `/deployment/` rules in CODEOWNERS |
| `--cd argocd` | + `deployment/argocd/application-{dev,staging,prod}.yaml`, `.github/workflows/{staging,promote-to-prod}.yaml` |
| `--cd helm-push` | + `.github/workflows/{staging,promote-to-prod}.yaml` |
| `--runtime langgraph-server` | server Dockerfile (`FROM langchain/langgraph-api:0.14.4-py3.12`, meta routes disabled), `langgraph.json` `http.app` + `auth`, Redis toggle in values, `uv-langgraph-server.lock` -> `uv.lock` |
| `--runtime fastapi` | multi-stage python Dockerfile with uvicorn (uid 1000, no uv in the final image), `uv-fastapi.lock` -> `uv.lock` |
| `--api-policy <file>` | `api-policy.yaml` at the root (copied into the image by the Dockerfile), `app/tools/example_api.py` (one call the first declared API allows, whatever its method: its first allowed operation, else one from its OpenAPI spec, else a generic operation of its first allowed method such as `GET /items/{item_id}` or `POST /items`; a `body` argument for POST, PUT and PATCH; left out, with a note, when that API allows nothing the example can make), `api_policy.policy_file` in the manifest, each `auth: bearer` API's `token_env` in `secrets.keys`, each API's `base_url_env` in `.env.example` and the chart values. `graph-agents-cli api add` makes the same changes for an API added later |
| `--auth-policy custom` | `auth_policy_implemented: false` (the `app/policies/custom.py` stub ships in every project) |
| `--auth-policy jwt` | `AUTH_JWT_*` settings in `.env.example` and the chart values |

## `graph-agents-cli scaffold enhance [TEMPLATE_PATH]`

`enhance` always enhances the current directory. `TEMPLATE_PATH` names the template to apply
(default: the current directory, re-rendering the recorded template); when the manifest records a
template it is re-applied and `TEMPLATE_PATH` is ignored.

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--name` | `-n` | the manifest's name (else the current directory name) | Project name for templating |
| `--deployment-target` | `-d` | manifest value | Add or change the target (`kubernetes`, `none`) |
| `--cd` | | manifest value | Add or change the CD mode (`argocd`, `helm-push`, `skip`) |
| `--runtime` | | manifest value | Change the runtime: the Dockerfile, `langgraph.json`, `secrets.keys`, `.env.example`, the chart values (key by key around your edits) and `.github/agent.env`; run `graph-agents-cli install` afterwards for `uv.lock` |
| `--checkpointer` | | manifest value | Change the deployed checkpointer default |
| `--registry` | | manifest value | Change the registry in values and workflows |
| `--auth-policy` | | manifest value | Switch between `shared-bearer`, `jwt` and `custom` |
| `--model-provider`, `--model` | | manifest value | Update the manifest, `secrets.keys`, `.env.example` and the chart values; the model follows the new provider's default only when it was the old default (otherwise pass `--model`, or exit 2); `.env` is never rewritten |
| `--process` | | manifest value | Declare or change the governing process |
| `--prototype` | `-p` | off | Same semantics as on `create` |
| `--agent-directory` | `-dir` | `app` | Where the agent code lives; pass it when not `app/` |
| `--agent-guidance-filename` | | the manifest's value | Guidance file to render |
| `--base-template` | `-bt` | | Base template underneath `TEMPLATE_PATH` (remote templates only) |
| `--force` | | off | Overwrite all scaffolding files (skips the 3-way compare; agent code and config still untouched); the same reconciliation runs afterwards, and a replay's exit code is kept |
| `--dry-run` / `--dryrun` | | off | Preview the merge without applying (requires saved metadata) |
| `--prefer-new` | | off | Resolve scaffolding conflicts in favour of the new template |
| `--skip-checks` | `-s` | off | Skip the `uv` preflight |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive |
| `--interactive` | `-i` | off | Prompts |
| `--debug` | | off | Debug logging |

`enhance` never touches `api-policy.yaml` (it refuses `--api-policy`, exit 2: change the policy
with `graph-agents-cli api`), `.env`, or agent code; files in those categories that
the project does not have yet but the new template does are added. Config files the new settings
re-render (`.env.example`, `values-*.yaml`, `deployment/argocd/**`) take the new render when
untouched and get the change merged in when edited; the chart's `values.yaml` and
`.github/agent.env` are merged key by key. When the merge changes the manifest (for example
`enhance --cd argocd` updating `cd:`), `graph-agents-cli-manifest.yaml` is rewritten through the
YAML dumper in block style and its comments are dropped.

Output: "Recomputed for the new settings", the files merged, and a numbered "Left for you" list.
Exit codes: `0` applied (anything left is optional), `1` applied with `(required)` steps left (a
chart key still on the old settings, `Dockerfile.new` beside an edited `Dockerfile`, dependency
changes uv could not write), `2` usage error, `3` configuration error (no project for
`--dry-run`, a legacy policy file). Backups: `~/.graph-agents-cli/backups/<dir>_<project
id>_<timestamp>` (0700; the newest 5 per project are kept).

## `graph-agents-cli scaffold upgrade [<path>]`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--dry-run` / `--dryrun` | | off | Show what would change |
| `--auto-approve` / `--yes` | `-y` | off | Apply non-conflicting changes without prompting |
| `--interactive` | `-i` | off | Resolve conflicts interactively |
| `--baseline authentic\|current` | | `authentic` | `authentic` runs the exact build that created the project through `uvx` and stops if it cannot; `current` compares against the current templates instead (explicit, logged, result labelled) |
| `--baseline-ref REF` | | none | The build that created the project, when the manifest cannot name it: a commit or tag of the repository, `<clone>@<commit>` for a local clone, a path to a checkout or wheel, or a full install spec. Also upgrades a project of the running version. Not with `--baseline current` |
| `--debug` | | off | Debug logging |

Behaviour: requires `uvx`; runs `uvx --from <spec> graph-agents-cli scaffold create` to
regenerate the old baseline. The spec is `--baseline-ref`'s; else, for a manifest whose
`cli_build` names a build between releases (id `X.Y.Z+g<commit>`), that commit
(`git+https://github.com/ss7172/graph-agents-cli@<commit>`); else the `cli_version` release
(`git+https://github.com/ss7172/graph-agents-cli@v<version>`, or
`GRAPH_AGENTS_CLI_INSTALL_SPEC` with `{version}` filled in; `{version}` names releases only).
A local path is rebuilt (`uvx --refresh-package graph-agents-cli`). At the running version,
a project is "already at version" when `cli_build` names this build or one with the same
`template_digest`, and a manifest without `cli_build` is compared by version only (the message
says how to name its build with `--baseline-ref`). Stops with no changes when the baseline
fails (exit 2: `uvx` missing or the fetch failed; exit 3: `cli_version` missing or not a
release, an override without `{version}`, a recorded build with uncommitted changes, a build
between releases recorded while an override is set, a `--baseline-ref` that names no build, or
a baseline that renders another `cli_version`), unless `--baseline current`, which cannot tell
your edits from template changes since the old version (unedited files it preserves keep
their old content; dependency changes are not merged). Backup first to
`~/.graph-agents-cli/backups/`. Updates `cli_version` (when it changes) and `cli_build` in the
manifest on success. Outside a project: exit 3. A project still on the retired
`product-policy.yaml` stops with migration steps (exit 3).
