---
description: Override or add graph-agents-cli commands from an extension repository or a local path (experimental).
---

# Extensions

<p class="gac-lede">Override built-in commands or add new ones from an extension repository or
a local path: a compliance check before <code>deploy</code>, a team command, a different eval
transport. New third-party code needs an explicit trust decision.</p>

!!! warning "Experimental"

    The extension manifest format and the `extension` commands may still change in a breaking
    way. Pin the CLI version if you depend on them.

## What an extension can do

An extension is a directory holding a `graph-agents-cli-extension.yaml`, so any git repository
(or a folder on disk) can serve one. It changes commands; it cannot add a deployment target or
a framework.

| It can | Example |
|---|---|
| Add a command | `graph-agents-cli release-notes` |
| Override a top-level command | `deploy`, `lint`, `build` |
| Override one subcommand of a group, by its dotted name | `eval.generate`, `secrets.apply`, `infra.check` |

| It cannot | Because |
|---|---|
| Override a whole group (`eval`, `scaffold`, `secrets`, `infra`, ...) | the other subcommands keep their built-in behaviour; the CLI skips it with a warning |
| Override `install` or `extension` | they are how you repair or remove an extension |
| Add a command that already exists | use `override` |

Two overrides reach further than their name:

- **`eval run` honours both stage overrides.** Overriding `eval.generate` or `eval.grade`
  changes the composite `eval run` exactly as it changes the standalone command.
- **`create` and `scaffold create` are one command**: overriding `scaffold.create` takes over
  `create` too.

Whenever extension commands apply, every CLI call says so on one line, so a takeover is always
visible:

```text title="Output"
Warning: graph-agents-cli: applying 2 extension command(s): lint [team-tools/project], release-notes [team-tools/project]
```

## Use an extension

```bash
# Add an extension, pinned to a tag (recommended)
graph-agents-cli extension add acme/gacli-extensions#soc2 --ref v1.2.0
# What is active, in which scope, with which commands
graph-agents-cli extension list
# Advance the pin within the tracked ref
graph-agents-cli extension update soc2
# Drop it and its vendored copy
graph-agents-cli extension remove soc2
```

`graph-agents-cli info` also lists the active extensions, their sources and any conflicts.
`extension add REFERENCE` accepts:

| Reference | Meaning |
|---|---|
| `acme/gacli-extensions` | a repository on github.com |
| `acme/gacli-extensions#soc2` | one extension of a repository that holds several |
| `https://git.example.com/acme/gacli-extensions`, `git@git.example.com:acme/gacli-extensions` | any git host (`https://`, `http://`, `ssh://`, scp form) |
| `../my-extension`, `/abs/path`, `~/ext`, `local@<path>` | a local directory, for development |
| `--ref <ref>` | the branch, tag or commit SHA to pin |

A repository is cloned with your ambient git configuration; the CLI neither asks for nor stores
credentials. A bad local path is exit 3; a git or network failure is exit 2. A bare name
(`soc2`) is the first-party shorthand for an extension in the graph-agents-cli repository; none
are published there yet.

### Scopes

| Scope | Recorded in | Applies to |
|---|---|---|
| project (default) | `graph-agents-cli-extensions.yaml`, with a working copy vendored under `extensions/` | this repository: commit both, and teammates and CI get the same commands |
| user (`--global`) | `~/.config/graph-agents-cli/` (`%APPDATA%\graph-agents-cli` on Windows) | every project on the machine |

When both scopes define a command, the project wins; two extensions claiming one command in one
scope is first-wins, and `extension list` and `info` show the conflict. Prefer project scope
unless you want an extension machine-wide. A local path is recorded relative to the project
root (absolute with `--global`).

### Trust

Every reference except the first-party shorthand is third-party code that runs on your machine
when its commands are invoked, so `add` asks first. `-y` trusts it without asking; use it only
for automation you control. Without a terminal to ask on (CI, a pipe), `add` never prompts:

```text title="Output"
  Extension source 'local@../team-tools' is third-party. It can run arbitrary code on your machine when its commands are invoked.
  Not asking: stdin is not a terminal. Pass -y to trust this extension non-interactively.
Error: Aborted: extension not trusted.
```

`add` then exits 1. `extension update` resolves the tracked ref first: an extension whose code
did not change is "Already up to date" without a prompt, and new code needs your trust again
(without a terminal it keeps the installed copy and exits 1).

!!! danger "Project extensions run with the trust of the repository you are in"

    A project-scope extension, or an ad-hoc `graph-agents-cli-extension.yaml` at the project
    root, loads automatically for anyone who runs the CLI in that repository. Review changes
    to them like code. The generated `.github/CODEOWNERS` covers
    `graph-agents-cli-extensions.yaml` and `extensions/`; add the ad-hoc file to it if you
    use one.

### Pinning and updates

- `extension add` resolves the ref to an exact commit and records `source`, `ref` and `sha` in
  `graph-agents-cli-extensions.yaml`.
- `graph-agents-cli install` restores a missing or stale vendored copy from the pinned commit.
  It never advances a pin.
- `extension update [NAME]` advances pins to the latest commit of the tracked ref (every
  extension without `NAME`). A pinned tag or commit resolves to itself: to move to another tag,
  run `extension add` again with the new `--ref`.
- A failed re-`add` leaves nothing installed rather than the previous pin: re-add the old ref
  to restore it.

## Write an extension

This example adds a `release-notes` command and makes `lint` refuse `TODO` markers before
running the built-in lint. The directory:

```text title="Extension layout"
team-tools/
├── graph-agents-cli-extension.yaml
└── scripts/
    ├── lint.sh
    └── release_notes.py
```

```yaml title="team-tools/graph-agents-cli-extension.yaml"
# yaml-language-server: $schema=https://raw.githubusercontent.com/ss7172/graph-agents-cli/main/schemas/graph-agents-cli-extension-v1alpha1.schema.json
schema: graph-agents-cli-extension/v1alpha1
name: team-tools
description: A release-notes command and a stricter lint.
requires:
  agents_cli: ">=0.2,<0.3"
  on_incompatible: warn
commands:
  add:
    release-notes:
      run: ["python3", "scripts/release_notes.py"]
      description: Print the release notes for a version.
  override:
    lint:
      run: ["bash", "scripts/lint.sh"]
      description: Refuse TODO markers in app/, then run the built-in lint.
```

```python title="team-tools/scripts/release_notes.py"
import sys

version = sys.argv[1] if len(sys.argv) > 1 else "unreleased"
print(f"Release notes for {version}")
```

```bash title="team-tools/scripts/lint.sh"
#!/usr/bin/env bash
set -euo pipefail
if grep -rn "TODO" app/ --include='*.py'; then
  echo "team-tools: resolve the TODO markers above first" >&2
  exit 1
fi
exec graph-agents-cli lint "$@"   # overrides are off here: this is the built-in lint
```

Add it to a project and use it:

```bash
graph-agents-cli extension add ../team-tools -y
graph-agents-cli release-notes v1.4.0
graph-agents-cli lint
```

```text title="Output"
Added extension 'team-tools' (project scope) from local@../team-tools.
Extensions are experimental; the manifest format may still change.
Warning: graph-agents-cli: applying 2 extension command(s): lint [team-tools/project], release-notes [team-tools/project]
Release notes for v1.4.0
```

`--help` lists both commands, each marked `[↑ team-tools]`. To share the extension, move the
directory into its own git repository, tag it, and others run `graph-agents-cli extension add
<org>/<repo>#team-tools --ref v1.0.0`: the file does not change.

For one extension that lives in the project itself, skip `extension add`: a
`graph-agents-cli-extension.yaml` at the project root (next to the manifest) is loaded at project
scope automatically. Only one, at that exact path.

### How commands run

- **`run:` is a command vector, run with no shell.** The user's arguments are appended
  verbatim. Start it with a program (`python3`, `bash`, `uv run ...`), not a bare script path,
  which relies on a shebang and never runs on Windows.
- **Paths resolve against the extension.** A `run:` token written as a path (`scripts/lint.sh`)
  that exists in the extension directory is made absolute, so the script is found from any
  working directory. `$GRAPH_AGENTS_CLI_EXTENSION_DIR` also names that directory, for sibling
  files.
- **The command runs in the project root** and exits with the child's exit code.
- **Calling the built-in is safe.** An extension command runs with
  `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`, so `graph-agents-cli lint` inside the wrapper hits the
  built-in, with no recursion. Chain steps in a wrapper script, since `run:` is one vector.

### The manifest

| Key | Required | Meaning |
|---|---|---|
| `schema` | no | `graph-agents-cli-extension/v1alpha1`: the manifest format, not the CLI version |
| `name` | no | the extension's name (defaults to its directory or repository name) |
| `description` | no | what the extension does |
| `requires.agents_cli` | no, but always set it | the CLI versions it supports, for example `">=0.2,<0.3"` |
| `requires.on_incompatible` | no | `warn` (default: install, run, warn when out of range) or `error` (`add` and `update` refuse; if a CLI upgrade leaves the range, its commands fail with the range and the fix instead of silently running the built-in) |
| `commands.add.<name>` | | a new command: `run` (a non-empty list) and an optional `description` |
| `commands.override.<name>` | | replace a built-in: `lint`, or `eval.generate` for a subcommand |

Unknown keys are refused, so a typo fails loudly. The machine-readable schema is
`schemas/graph-agents-cli-extension-v1alpha1.schema.json` in the graph-agents-cli repository;
point a `yaml-language-server` modeline at it, as above, for validation in your editor.

Set the lower bound of `requires.agents_cli` to the `major.minor` that `graph-agents-cli
--version` prints, and the upper bound to the next minor while the CLI is 0.x (a 0.x minor
release may break compatibility), the next major from 1.0 on.

## In CI and CD

`GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1` makes the CLI ignore every extension override. Every
generated CI and CD job sets it, so the `pr_checks` gate always runs the CLI's own `lint` and
`eval run` ([CI/CD](cicd.md)). Set it yourself to run a built-in once, for example when an
override is broken:

```bash
GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1 graph-agents-cli lint
```

[Environment variables](../reference/environment.md) lists the CLI's other variables.

## Next steps

<div class="grid cards" markdown>

-   :material-console:{ .lg } **[`graph-agents-cli extension`](../reference/cli.md#graph-agents-cli-extension)**

    Every subcommand and flag.

-   :material-source-pull:{ .lg } **[CI/CD](cicd.md)**

    The generated workflows, which run with overrides disabled.

-   :material-variable:{ .lg } **[Environment variables](../reference/environment.md)**

    `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES` and the CLI's other settings.

</div>
