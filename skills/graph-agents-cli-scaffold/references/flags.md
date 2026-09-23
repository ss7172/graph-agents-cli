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
| `--api-policy` | | none | Path to an `api-policy.yaml` to seed at the project root (validated with the strict schema first, exit 3 on errors); adds `api_policy.policy_file` to the manifest. `--product-policy` is refused with a rename hint |
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
- `--deployment-target none` defaults `--checkpointer memory`.

What each choice renders:

| Choice | Files |
|---|---|
| (always) | `.github/workflows/pr_checks.yaml`, `.github/agent.env` (`GRAPH_AGENTS_CLI_SPEC`) |
| `--deployment-target kubernetes` | `deployment/helm/<name>/**`, `environments:` in the manifest, chart settings in `.github/agent.env` |
| `--cd argocd` | + `deployment/argocd/application-{dev,staging,prod}.yaml`, `.github/workflows/{staging,promote-to-prod}.yaml`, `.github/CODEOWNERS` |
| `--cd helm-push` | + `.github/workflows/{staging,promote-to-prod}.yaml`, `.github/CODEOWNERS` |
| `--runtime langgraph-server` | server Dockerfile (`FROM langchain/langgraph-api:<pinned>`), `langgraph.json` `http.app` + `auth`, Redis toggle in values, `uv-langgraph-server.lock` -> `uv.lock` |
| `--runtime fastapi` | python Dockerfile with uvicorn, `uv-fastapi.lock` -> `uv.lock` |
| `--api-policy <file>` | `api-policy.yaml` at the root (copied into the image by the Dockerfile), `app/tools/example_api.py` (calls the first declared API), `api_policy.policy_file` in the manifest, each `auth: bearer` API's `token_env` in `secrets.keys`, each API's `base_url_env` in `.env.example` and the chart values |
| `--auth-policy custom` | `auth_policy_implemented: false` (the `app/policies/custom.py` stub ships in every project) |
| `--auth-policy jwt` | `AUTH_JWT_*` settings in `.env.example` and the chart values |

## `graph-agents-cli scaffold enhance [TEMPLATE_PATH]`

`enhance` always enhances the current directory. `TEMPLATE_PATH` names the template to apply
(default: the current directory, re-rendering the recorded template); when the manifest records a
template it is re-applied and `TEMPLATE_PATH` is ignored.

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--name` | `-n` | current directory name | Project name for templating |
| `--deployment-target` | `-d` | manifest value | Add or change the target (`kubernetes`, `none`) |
| `--cd` | | manifest value | Add or change the CD mode (`argocd`, `helm-push`, `skip`) |
| `--runtime` | | manifest value | Change the runtime (re-renders the Dockerfile, `langgraph.json`, lock) |
| `--checkpointer` | | manifest value | Change the deployed checkpointer default |
| `--registry` | | manifest value | Change the registry in values and workflows |
| `--auth-policy` | | manifest value | Switch between `shared-bearer`, `jwt` and `custom` |
| `--model-provider`, `--model` | | manifest value | Update the manifest and `.env.example`; `.env` is never rewritten |
| `--api-policy` | | | Accepted by the parser but **refused** by `enhance` (exit with a message): copy the file into the project root as `api-policy.yaml` and set `api_policy.policy_file` in the manifest instead |
| `--process` | | manifest value | Declare or change the governing process |
| `--prototype` | `-p` | off | Same semantics as on `create` |
| `--agent-directory` | `-dir` | `app` | Where the agent code lives; pass it when not `app/` |
| `--agent-guidance-filename` | | the manifest's value | Guidance file to render |
| `--base-template` | `-bt` | | Base template underneath `TEMPLATE_PATH` (remote templates only) |
| `--force` | | off | Overwrite all scaffolding files (skips the 3-way compare; agent code and config still untouched) |
| `--dry-run` / `--dryrun` | | off | Preview the merge without applying (requires saved metadata) |
| `--prefer-new` | | off | Resolve scaffolding conflicts in favour of the new template |
| `--skip-checks` | `-s` | off | Skip the `uv` preflight |
| `--auto-approve` / `--yes` | `-y` | off | Non-interactive |
| `--interactive` | `-i` | off | Prompts |
| `--debug` | | off | Debug logging |

`enhance` never touches `api-policy.yaml`, `.env*`, `values-<env>.yaml`, or agent code; files
in those categories that the project does not have yet but the new template does are added.
When the merge changes the manifest (for example `enhance --cd argocd` updating `cd:`),
`graph-agents-cli-manifest.yaml` is rewritten through the YAML dumper in block style and its
comments are dropped; `.github/agent.env` is updated in the same run.

## `graph-agents-cli scaffold upgrade [<path>]`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--dry-run` / `--dryrun` | | off | Show what would change |
| `--auto-approve` / `--yes` | `-y` | off | Apply non-conflicting changes without prompting |
| `--interactive` | `-i` | off | Resolve conflicts interactively |
| `--baseline authentic\|current` | | `authentic` | `authentic` runs the exact prior CLI version through `uvx` and stops if it cannot; `current` compares against the current templates instead (explicit, logged, result labelled) |
| `--debug` | | off | Debug logging |

Behaviour: requires `uvx`; runs `uvx --from <install spec of the manifest cli_version> graph-agents-cli scaffold create`
to regenerate the old baseline; stops with a non-zero exit and no changes when that fails, unless
`--baseline current`. Backup first to `~/.graph-agents-cli/backups/`. Updates `cli_version` in
the manifest on success.
