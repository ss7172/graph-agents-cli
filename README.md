<div align="center">

# graph-agents-cli

**Build, evaluate and deploy [LangGraph](https://langchain-ai.github.io/langgraph/) agents on
your own Kubernetes, with one CLI and six skills for your coding agent.**

[![CI](https://github.com/ss7172/graph-agents-cli/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/ss7172/graph-agents-cli/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![Release](https://img.shields.io/github/v/release/ss7172/graph-agents-cli?sort=semver)](https://github.com/ss7172/graph-agents-cli/releases)
[![PyPI](https://img.shields.io/pypi/v/graph-agents-cli)](https://pypi.org/project/graph-agents-cli/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/ss7172/graph-agents-cli/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-site-0f766e)](https://ss7172.github.io/graph-agents-cli/)

[Get started](https://ss7172.github.io/graph-agents-cli/getting-started/) ·
[Guides](https://ss7172.github.io/graph-agents-cli/guides/) ·
[Reference](https://ss7172.github.io/graph-agents-cli/reference/) ·
[Changelog](https://github.com/ss7172/graph-agents-cli/blob/main/CHANGELOG.md)

</div>

## What you get

- **A LangGraph service, ready to run**: a streaming chat API, threads, an A2A endpoint,
  answers in a JSON shape you declare when a program reads them, and shared-key, OIDC/JWT or
  custom authentication on every route.
- **Outbound calls under a policy**: `api-policy.yaml` declares the APIs tools may call, the
  agent refuses anything else, `lint` checks it in CI, and chosen calls wait for a human
  approval.
- **Agents that ask other agents**: `peer add` declares the agents one asks over A2A, each
  agent knows the user and the agent in between, a person's approvals stay with that person,
  and `system` checks, wires and deploys several projects as one.
- **An eval gate CI can enforce**: deterministic checks and model judges, with an exit code
  that is the gate and a deterministic fake model for keyless runs.
- **Deploys to any cluster**: a hardened Helm chart, `deploy` straight from your machine or
  through GitHub Actions or Argo CD, and `secrets` for the app's Secret.
- **Works with your coding agent**: skills teach Claude Code, Codex, Gemini CLI, Cursor,
  Antigravity and others the same lifecycle.

Nothing is specific to one domain or one consumer: a project picks its auth policy, declares
its APIs and configures the rest through environment variables and chart values.

## Install

You need Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Deploying also needs `helm`, `kubectl`, `git` and a `docker` that builds with BuildKit.

```bash
uv tool install graph-agents-cli
graph-agents-cli setup      # optional: install the skills into your coding agents
```

`pipx install graph-agents-cli` or `pip install graph-agents-cli` (into a virtual
environment) work too. To install a release tag from GitHub instead:

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.3.0
```

More options (extras, mirrors, disconnected installs):
[Installation & setup](https://ss7172.github.io/graph-agents-cli/getting-started/installation/).

## Quick start

Run a new agent locally. The fake model needs no key; for a real one, leave out the
`export` line and `login --write-env` asks for `OPENAI_API_KEY`.

```bash
graph-agents-cli create my-agent && cd my-agent
cp .env.example .env
export MODEL_PROVIDER=fake            # the keyless, deterministic test model
graph-agents-cli login --write-env    # checks the setup, generates API_KEY
graph-agents-cli install
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli eval run             # the exit code is the gate
```

Or ask your coding agent to "use graph-agents-cli to build ..." once `setup` has installed
the skills. The [Quickstart](https://ss7172.github.io/graph-agents-cli/getting-started/quickstart/)
walks through each step and switches to a real provider.

## Documentation

The [documentation site](https://ss7172.github.io/graph-agents-cli/) is built with MkDocs
Material from [`website/`](https://github.com/ss7172/graph-agents-cli/blob/main/website/mkdocs.yml).

| Section | Pages |
|---|---|
| **Get started** | [Installation & setup](https://ss7172.github.io/graph-agents-cli/getting-started/installation/) · [Quickstart](https://ss7172.github.io/graph-agents-cli/getting-started/quickstart/) · [Tutorial: build with a coding agent](https://ss7172.github.io/graph-agents-cli/getting-started/tutorial-coding-agent/) · [Tutorial: manual workflow](https://ss7172.github.io/graph-agents-cli/getting-started/tutorial-manual/) · [The lifecycle](https://ss7172.github.io/graph-agents-cli/getting-started/lifecycle/) |
| **Build** | [Develop your agent](https://ss7172.github.io/graph-agents-cli/guides/develop/) · [Authentication](https://ss7172.github.io/graph-agents-cli/guides/authentication/) · [Outbound API policy](https://ss7172.github.io/graph-agents-cli/guides/api-policy/) · [Human approval](https://ss7172.github.io/graph-agents-cli/guides/approvals/) · [Agents calling agents](https://ss7172.github.io/graph-agents-cli/guides/multi-agent/) · [Evaluation](https://ss7172.github.io/graph-agents-cli/guides/evaluation/) · [Extensions](https://ss7172.github.io/graph-agents-cli/guides/extensions/) |
| **Operate** | [Deploy to Kubernetes](https://ss7172.github.io/graph-agents-cli/guides/deploy/) · [CI/CD](https://ss7172.github.io/graph-agents-cli/guides/cicd/) · [Secrets](https://ss7172.github.io/graph-agents-cli/guides/secrets/) · [Observability](https://ss7172.github.io/graph-agents-cli/guides/observability/) · [Upgrading projects](https://ss7172.github.io/graph-agents-cli/guides/upgrading/) · [Offline profile](https://ss7172.github.io/graph-agents-cli/guides/offline/) · [Security & production](https://ss7172.github.io/graph-agents-cli/guides/security/) |
| **Reference** | [CLI](https://ss7172.github.io/graph-agents-cli/reference/cli/) · [Environment variables](https://ss7172.github.io/graph-agents-cli/reference/environment/) · [HTTP API](https://ss7172.github.io/graph-agents-cli/reference/http-api/) · [api-policy.yaml](https://ss7172.github.io/graph-agents-cli/reference/api-policy-schema/) · [graph-agents-system.yaml](https://ss7172.github.io/graph-agents-cli/reference/system-file/) · [Project manifest](https://ss7172.github.io/graph-agents-cli/reference/manifest/) · [Exit codes](https://ss7172.github.io/graph-agents-cli/reference/exit-codes/) · [Skills](https://ss7172.github.io/graph-agents-cli/reference/skills/) · [Compared with google-agents-cli](https://ss7172.github.io/graph-agents-cli/reference/comparison/) |

To preview the site from a checkout, for example while editing it:

```bash
uv run --group docs mkdocs serve -f website/mkdocs.yml    # http://127.0.0.1:8200
```

On the command line, `graph-agents-cli <command> --help` prints the same reference as the
site's CLI page.

## Status

Version 0.3.0, **alpha**
([release notes](https://github.com/ss7172/graph-agents-cli/releases/tag/v0.3.0)).
Interfaces may still change between minor versions; the changelog lists every breaking
change with its migration steps. Released on
[PyPI](https://pypi.org/project/graph-agents-cli/) and as tags on GitHub.

- [Known issues](https://github.com/ss7172/graph-agents-cli/blob/main/KNOWN_ISSUES.md): parked issues, each with its
  impact and workaround.
- [Changelog](https://github.com/ss7172/graph-agents-cli/blob/main/CHANGELOG.md): every release and its migration
  steps.
- [Contributing](https://github.com/ss7172/graph-agents-cli/blob/main/CONTRIBUTING.md): development setup, tests,
  templates, releases and the upstream-sync process.

## License

Apache-2.0: see [LICENSE](https://github.com/ss7172/graph-agents-cli/blob/main/LICENSE). graph-agents-cli is a
fork of [google-agents-cli](https://github.com/google/agents-cli) with the Google Cloud
specific parts removed; [NOTICE](https://github.com/ss7172/graph-agents-cli/blob/main/NOTICE) lists the attribution
and the modifications.
