# Extensions: override or extend graph-agents-cli

An extension overrides or adds a built-in command. It is a directory containing a
`graph-agents-cli-extension.yaml`, so any git repo can serve as a registry.

Two things you can do: **author** an extension (start ad-hoc in the current repo, publish it
later), or **adopt** an existing one. Extensions change commands; they cannot add a deployment
target or a framework.

---

## Author an ad-hoc extension (no separate repo)

Drop a single file, `graph-agents-cli-extension.yaml`, at the project root (next to
`graph-agents-cli-manifest.yaml`). It is auto-loaded at project scope, so no `extension add` is
needed. Commit it and teammates and CI get the same overrides.

Only one, at that exact path, and always project scope. For a second one, or to install a local
extension globally, move it into its own directory and
`graph-agents-cli extension add local@./path --global`; `#name` picks one out of a directory
holding several.

**Publish it for other repos:** move the same file (and its scripts) into its own git repo and tag
it; others then `graph-agents-cli extension add <org>/<repo>#<name> --ref v1.0.0`. The file does
not change; ad-hoc and shared are the same format.

### Schema by example (`graph-agents-cli-extension/v1alpha1`)

Everything below is optional except `run` on a command (a non-empty list). Unknown keys are
rejected, so a typo fails loudly.

Machine-readable equivalent: `schemas/graph-agents-cli-extension-v1alpha1.schema.json` in the
graph-agents-cli repo, generated from the models the loader uses. Point a `yaml-language-server`
modeline at it for editor validation.

```yaml
schema: graph-agents-cli-extension/v1alpha1
name: my-extension
description: What this extension does.
requires:
  agents_cli: ">=0.2,<0.3"  # derive from `graph-agents-cli --version`, see below
  on_incompatible: warn     # warn (install + warn) | error (refuse at add/update, block its commands if the CLI drifts out)

commands:
  override:               # replace a built-in; user argv passes through verbatim
    deploy:
      run: ["uv", "run", "scripts/custom_deploy.py"]
      description: SBOM upload and a change-ticket check, then the built-in deploy.
    eval.generate:        # dotted name = a subcommand (group.sub)
      run: ["uv", "run", "scripts/eval_generate.py"]
      description: Drive a different chat transport for traces.
  add:                    # a brand-new command
    compliance-report:
      run: ["python", "scripts/compliance_report.py"]
      description: Generate the quarterly compliance report.
```

### Rules that matter

- **`run:` is a command vector** executed with **no shell**; user argv is appended verbatim. Paths
  relative to the extension dir resolve to absolute, and `$GRAPH_AGENTS_CLI_EXTENSION_DIR` locates
  sibling scripts and templates.
- **You cannot override a command group** (`eval`, `scaffold`, `secrets`, `infra`); override a
  specific subcommand (`eval.generate`, `scaffold.create`, `secrets.apply`, `infra.check`). Peers
  keep their built-in behaviour. `install` and `extension` can never be overridden.
- **`eval run` honours both stage overrides.** Overriding `eval.generate` or `eval.grade` changes
  the composite `eval run` exactly as it changes the standalone command.
- **`create` and `scaffold create` are one command:** overriding `scaffold.create` takes over the
  `create` alias too.
- **Re-invoke the built-in safely.** An override runs with `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`,
  so calling `graph-agents-cli deploy` inside your wrapper hits the built-in (no recursion).
- **Chaining is done in a wrapper script**, since `run:` is a single vector:
  ```bash
  #!/usr/bin/env bash
  set -e
  "$GRAPH_AGENTS_CLI_EXTENSION_DIR/scripts/change_ticket_check.sh"   # non-zero here aborts
  graph-agents-cli deploy "$@"                                         # hits the built-in
  ```
- **Start `run:` with a program, not a script.** `["python", "scripts/x.py"]` or
  `["uv", "run", "python", "scripts/x.py"]` works everywhere; a bare `["scripts/x.py"]` relies on a
  shebang and never runs on Windows (the CLI warns).
- **Conflicts** (same scope, shown in `extension list` and `info`): two extensions claiming one
  command is first-wins. Cross-scope is fine; project wins over user (`--global`).
- **Declare a compatibility range** with `requires`, always. Run `graph-agents-cli --version` and
  set the lower bound to that `major.minor`. The upper bound is the next minor while the CLI is
  0.x (a 0.x minor release may break compatibility: `>=0.2,<0.3`), the next major from 1.0 on. Let the user pick
  `on_incompatible`; default `warn`.
  - `warn`: installs, runs, warns when out of range.
  - `error`: `extension add`/`update` refuse an out-of-range install, and if a later CLI upgrade
    moves you out of range the extension's commands fail with the range and the fix rather than
    silently running the built-in. The recovery commands (`install`, `extension *`) keep working.
  - `schema` tracks the manifest format, not the CLI version.

---

## Adopt an existing extension

```bash
graph-agents-cli extension add <ref> [--global] [--ref <branch|tag|sha>] [--yes]
graph-agents-cli extension list                 # what's active, its scope, and its commands
graph-agents-cli extension update [<name>]      # advance the pin (re-resolve the tracked ref)
graph-agents-cli extension remove <name>        # drop it and delete its vendored copy
graph-agents-cli info                           # active extensions + sources + conflicts
```

### Reference forms

| Form | Meaning |
|------|---------|
| `acme/gacli-extensions` | any `org/repo` on github.com |
| `acme/gacli-extensions#soc2-deploy` | select one extension from a multi-extension repo |
| `https://git.example.com/acme/gacli-extensions` | any git host: `https://`, `http://`, or `ssh://` |
| `git@git.example.com:acme/gacli-extensions` | the same host, scp form |
| `../my-extension`, `./ext`, `/abs/path`, `~/ext`, `C:\ext`, or `local@<path>` | a local directory (for development); recorded relative to the project root (absolute with `--global`) and resolved from there by `install` and `update`. A bad local path is exit 3; a git or network failure resolving a repository is exit 2 |
| `<name>` | first-party shorthand (resolves to the graph-agents-cli repository) |
| `--ref <branch\|tag\|sha>` | pin a branch, tag, or commit SHA |

A URL is cloned with your ambient git configuration; graph-agents-cli neither asks for nor stores
credentials. On a disconnected network, extensions come from an on-network git host or
`local@` paths.

### Scopes

- **Project scope (default):** recorded in `graph-agents-cli-extensions.yaml`, working copy
  vendored under `extensions/`. Commit both so it works offline (`install` re-fetches if missing).
- **User scope (`--global`):** `~/.config/graph-agents-cli/`. Applies to every project on the
  machine. Prefer project scope unless you truly want it machine-wide.
- When both scopes define the same command, **project wins**; `info` shows the source.

### Trust

- First-party extensions added via the shorthand form are trusted automatically.
- Every other reference prompts before install (its commands run arbitrary code when invoked).
  `--yes` skips the prompt; use it only for automation.

### Pinning and updates

- `extension add` resolves the ref to an exact commit SHA and records `source` / `ref` / `sha`
  under `extensions:` in `graph-agents-cli-extensions.yaml`, vendoring a working copy under
  `extensions/`.
- `graph-agents-cli install` re-materializes any missing or stale vendored copy from the pinned
  SHA. It never advances a pin.
- `extension update [name]` advances the pin to the latest commit of the same tracked ref. A pinned
  tag or SHA re-resolves to itself; to move to a different tag, re-run `extension add <ref>
  --ref <new-tag>`.
- `extension remove <name>` removes from one scope per call (project before user).

```bash
graph-agents-cli extension add acme/gacli-extensions#soc2 --ref v1.2.0   # pin a tag (recommended)
graph-agents-cli extension add acme/gacli-extensions#soc2 --ref main     # follow a branch
graph-agents-cli extension update soc2                                   # advance within the tracked ref
graph-agents-cli extension add acme/gacli-extensions#soc2 --ref v1.3.0   # move to another tag: re-add
```

A failed re-`add` rolls back to nothing rather than to the previous pin, so re-add the old ref to
restore it.
