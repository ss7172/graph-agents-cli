---
description: Install graph-agents-cli from PyPI, add the skills to your coding agents and check your environment with login.
---

# Installation & setup

<p class="gac-lede">Install the CLI from PyPI, give your coding agents the six
skills, and let <code>login</code> tell you what is still missing before you create a
project.</p>

!!! success "Released on PyPI"
    graph-agents-cli is on [PyPI](https://pypi.org/project/graph-agents-cli/) from 0.3.1:
    `uv tool install graph-agents-cli`. Earlier releases (0.1.0 to 0.3.0) are git tags only.
    The six skills it installs are tuned with SkillOpt; see the
    [skills benchmark](../reference/skills-benchmark.md).

## Prerequisites

You need Python and uv for everything; the rest only for the stage that uses it.

| Tool | Needed for |
|---|---|
| Python 3.12 or 3.13 | The CLI and every project it generates |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installing the CLI; `install`, `run`, `playground`, `lint` and `eval` run the project through it |
| Node.js (`npx`) | `setup` and `update` install the skills with `npx skills`; without it `setup` copies them instead |
| `helm`, `kubectl`, a Docker-compatible `docker` CLI that builds with BuildKit, `git` | `build`, `deploy` and `secrets`. The generated Dockerfile uses `RUN --mount`, which needs BuildKit (the `buildx` plugin; the default in Docker Desktop) |
| `gh` | Argo CD mode (`deploy` opens pull requests) and GitHub-hosted CD |

Everything up to deployment runs on your machine. A tool that `deploy` needs and cannot
find on `PATH` makes it exit 2.

## Install the CLI

Install the [PyPI package](https://pypi.org/project/graph-agents-cli/) with `uv tool`:

```bash
uv tool install graph-agents-cli
graph-agents-cli --version
```

```text
graph-agents-cli, version 0.3.1
```

`uv tool upgrade graph-agents-cli` moves to the latest release later. Other installers work
too:

=== "pipx"

    ```bash
    pipx install graph-agents-cli
    ```

=== "pip"

    ```bash
    python -m venv ~/.venvs/graph-agents-cli
    ~/.venvs/graph-agents-cli/bin/pip install graph-agents-cli
    ```

    Put `~/.venvs/graph-agents-cli/bin` on your `PATH`, or call the CLI by its full path.

=== "A release tag from GitHub"

    ```bash
    uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.3.1
    ```

    The same release, built from its git tag: what `setup`, `update` and generated projects'
    CI install (see [Install sources](#install-sources)).

### Optional extras

Two commands need an extra dependency:

| Extra | For |
|---|---|
| `a2a` | `run --mode a2a` (talk to an agent over A2A JSON-RPC) |
| `langsmith` | `eval submit` (upload a dataset and results to LangSmith) |

```bash
uv tool install 'graph-agents-cli[a2a,langsmith]'
# or from the release tag
uv tool install 'graph-agents-cli[a2a,langsmith] @ git+https://github.com/ss7172/graph-agents-cli@v0.3.1'
```

### Which build you are running

`--version` names the build, and `info` adds the full commit:

| `--version` prints | Meaning |
|---|---|
| `0.3.1` | The release, built from the `v0.3.1` tag |
| `0.3.1+g<commit>` | A build of another commit (a checkout between releases) |
| `0.3.1+g<commit>.dirty` | A build with uncommitted changes |

Every project records the build that created it, which is what
[`scaffold upgrade`](../guides/upgrading.md) replays later. To install a build from a
source checkout, see
[Installing a build from a checkout](https://github.com/ss7172/graph-agents-cli/blob/main/CONTRIBUTING.md#installing-a-build-from-a-checkout)
in CONTRIBUTING.md.

## Install the skills

`setup` installs the six [skills](../reference/skills.md) into the coding agents it finds
(Claude Code, Codex, Gemini CLI, Cursor, Antigravity and others), so you can ask your agent
to "use graph-agents-cli to build ...". It also runs `uv tool install` for the pinned CLI
from its release tag: nothing changes when that is already installed, and a CLI installed
from PyPI is replaced by the same release built from the tag (see
[Install sources](#install-sources)).

=== "Every detected agent"

    ```bash
    graph-agents-cli setup
    ```

    Installs globally for every coding agent `npx skills` detects.

=== "Chosen agents"

    ```bash
    graph-agents-cli setup --agent claude-code --agent cursor
    ```

    Repeat `--agent` for each one; `--agent all` installs for every agent that
    `npx skills` supports.

=== "This workspace only"

    ```bash
    graph-agents-cli setup --workspace
    ```

    Installs into the current directory instead of your home directory.

Preview any of these with `--dry-run`, which prints the commands and changes nothing:

```bash
graph-agents-cli setup --dry-run
```

<div class="gac-terminal" markdown>

```text
 1. Dry Run
 ──────────

  Would install graph-agents-cli:
  ▸ uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.3.1

  Would install skills:
  ▸ npx -y skills@1.5.9 add 'https://github.com/ss7172/graph-agents-cli#v0.3.1' -y -g
    (falls back to the bundled skills, then to a copy into ~/.agents/skills)
  Scope: global

  No changes made (dry run).
```

</div>

### Where the skills come from

The skills match the release your CLI's version names. `setup` tries three sources in
order, each only when the one before it fails:

1. `npx skills add` from this repository at that release's tag
   (`https://github.com/ss7172/graph-agents-cli#v0.3.1`). Needs `git` and network access.
2. `npx skills add` from the copy bundled in the installed CLI (same build, no network).
3. A plain copy of the bundled skills into `~/.agents/skills` (`./.agents/skills` with
   `--workspace`), for machines without Node.js.

A build between releases (`0.3.1+g<commit>`) still installs the `v0.3.1` skills in step 1.
Only a version with no release behind it (`0.0.0`, a `.devN` or a `+local` version) uses the
default branch. For skills that match a checkout's own code, run `setup --dev` from the
checkout (it also installs the CLI from it, editable) or pass `--skills-source <checkout>`.

`--skills-source` names another source instead: a local path, a GitHub `owner/repo`, or a
URL with a `#<ref>`. An explicit source never falls back to the bundled copy.

!!! note "Antigravity"
    A global `setup` also links the skills into the directories Antigravity reads
    (`~/.gemini/config/skills` and `~/.gemini/antigravity-cli/skills`) when `~/.gemini`
    exists, because `npx skills` installs global skills into `~/.agents/skills`.

The CLI stores no credentials: `setup` never asks for a key. See
[`setup`](../reference/cli.md#graph-agents-cli-setup) in the CLI reference for every flag,
including `--dev` for contributors.

## Keep up to date

```bash
graph-agents-cli update
```

`update` refreshes the installed skills, then reinstalls the CLI from the latest GitHub
release when it is newer than the one you run (best effort: offline, it leaves the CLI as
it is) and moves the skills to that release's tag, so the two stay in step. Add `-i` to
confirm before it starts.

The CLI also checks GitHub for a newer release at most once every 12 hours and prints an
"Update available" line when there is one. Set `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` to turn
the check off, for example on a machine without internet access.

## Check your environment

`login` is a preflight, not a sign-in: it reads the process environment and the project's
`.env`, reports what is missing, and stores nothing. Run it inside a project; the
[Quickstart](quickstart.md) does that right after `create`.

```bash
graph-agents-cli login --write-env
```

| Check | Passes when |
|---|---|
| `provider`, `provider_key` | The provider's key is set (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY`); for `openai-compatible`, `OPENAI_BASE_URL` is set (whether it answers is reported as advice) and `MODEL_API_KEY` is optional |
| `api_key` | Under the `shared-bearer` auth policy, `API_KEY` is set (without it the local server answers 503) |
| `jwt_key`, `jwt_token` | Under `jwt`, a verification key is set and `GRAPH_AGENTS_CLI_API_KEY` holds a token for `run` and `eval` |
| `env_file` | `.env` is not readable by other users |
| `judge` | The eval judge's provider and key, when `JUDGE_MODEL_PROVIDER` is set |
| `tracing` | With `TRACING_ENABLED=true`, `LANGSMITH_API_KEY` or an OTLP endpoint is set |
| `kubeconfig` | `kubectl` has a current context (`--cluster` also checks that the cluster answers) |

`--write-env` fixes what it can: it prompts for missing keys without echoing them, fills
blank `KEY=` lines of `.env` in place, generates an `API_KEY` for a `shared-bearer` project
and leaves `.env` at mode 0600.

| Flag | Effect |
|---|---|
| `--status` | Print the report and exit 0 even when a check fails |
| `--json` | Print the report as JSON |
| `--cluster` | Also run `kubectl cluster-info` against the current context |
| `--profile disconnected` | Fail on every hosted dependency (see [Offline profile](../guides/offline.md)) |
| `--env-file FILE` | Read (and write) another env file |

Without `--status`, a failed check makes `login` exit 1, so a script or a coding agent can
stop there.

## Install sources

`setup`, `update`, the `scaffold upgrade` baseline and the CI of every generated project
(`GRAPH_AGENTS_CLI_SPEC` in its `.github/agent.env`) install the CLI from the same pinned
git tag, whichever way you installed it: every release is tagged, while releases before
0.3.1 are not on PyPI. `GRAPH_AGENTS_CLI_INSTALL_SPEC` points all of
them somewhere else: a private mirror, a wheel, or a package index. Write `{version}` where
the release number goes (`git+https://git.example.com/graph-agents-cli@v{version}`) so an
upgrade can install an older release; an override that cannot work is refused with exit 3.
`GRAPH_AGENTS_CLI_INSTALL_SPEC='graph-agents-cli=={version}'` installs from PyPI (or the
index `UV_INDEX_URL` names), for releases published there.
[Environment variables](../reference/environment.md) has the full rules, and
[Offline profile](../guides/offline.md) shows a complete disconnected setup.

## Next steps

<div class="grid cards gac-cols-3" markdown>

-   :material-rocket-launch-outline:{ .lg } **[Quickstart](quickstart.md)**

    Create a project and talk to your first agent in five minutes, without a model key.

-   :material-robot-outline:{ .lg } **[Build with a coding agent](tutorial-coding-agent.md)**

    Let the skills drive the lifecycle while you review each step.

-   :material-console:{ .lg } **[CLI reference](../reference/cli.md)**

    Every command and flag, generated from the CLI itself.

</div>
