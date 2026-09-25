<div align="center">

# graph-agents-cli

**Build, evaluate and deploy [LangGraph](https://langchain-ai.github.io/langgraph/) agents on
your own Kubernetes, with one CLI and six skills for your coding agent.**

[![CI](https://github.com/ss7172/graph-agents-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/ss7172/graph-agents-cli/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/ss7172/graph-agents-cli?sort=semver)](https://github.com/ss7172/graph-agents-cli/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Docs](https://github.com/ss7172/graph-agents-cli/actions/workflows/docs.yml/badge.svg)](website/src/index.md)

[Get started](website/src/getting-started/index.md) ·
[Guides](website/src/guides/index.md) ·
[Reference](website/src/reference/index.md) ·
[Changelog](CHANGELOG.md)

</div>

## What you get

- **A LangGraph service, ready to run**: a streaming chat API, threads, an A2A endpoint and
  shared-key, OIDC/JWT or custom authentication on every route.
- **Outbound calls under a policy**: `api-policy.yaml` declares the APIs tools may call, the
  agent refuses anything else, `lint` checks it in CI, and chosen calls wait for a human
  approval.
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
Deploying also needs `helm`, `kubectl`, `docker` and `git`.

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0
graph-agents-cli setup      # optional: install the skills into your coding agents
```

> [!NOTE]
> graph-agents-cli is not on PyPI yet. Until this repository's release workflow publishes
> it, a package named `graph-agents-cli` on an index is not this project: install from the
> release tag as above.

More options (extras, mirrors, disconnected installs):
[Installation & setup](website/src/getting-started/installation.md).

## Quick start

Run a new agent locally. `MODEL_PROVIDER=fake` in `.env` needs no model key.

```bash
graph-agents-cli create my-agent && cd my-agent
cp .env.example .env                  # set OPENAI_API_KEY, or MODEL_PROVIDER=fake to try it keyless
graph-agents-cli login --write-env    # checks the setup, generates API_KEY
graph-agents-cli install
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli eval run             # the exit code is the gate
```

Or ask your coding agent to "use graph-agents-cli to build ..." once `setup` has installed
the skills. The [Quickstart](website/src/getting-started/quickstart.md) walks through each
step and switches to a real provider.

## Documentation

The documentation is a site in [`website/`](website/src/index.md), built with MkDocs
Material. It will be published at `https://ss7172.github.io/graph-agents-cli/` (not yet
enabled); until then, read the pages here or preview the site:

```bash
uv run --group docs mkdocs serve -f website/mkdocs.yml    # http://127.0.0.1:8200
```

| Section | Pages |
|---|---|
| **Get started** | [Installation & setup](website/src/getting-started/installation.md) · [Quickstart](website/src/getting-started/quickstart.md) · [Tutorial: build with a coding agent](website/src/getting-started/tutorial-coding-agent.md) · [Tutorial: manual workflow](website/src/getting-started/tutorial-manual.md) · [The lifecycle](website/src/getting-started/lifecycle.md) |
| **Build** | [Develop your agent](website/src/guides/develop.md) · [Authentication](website/src/guides/authentication.md) · [Outbound API policy](website/src/guides/api-policy.md) · [Human approval](website/src/guides/approvals.md) · [Evaluation](website/src/guides/evaluation.md) · [Extensions](website/src/guides/extensions.md) |
| **Operate** | [Deploy to Kubernetes](website/src/guides/deploy.md) · [CI/CD](website/src/guides/cicd.md) · [Secrets](website/src/guides/secrets.md) · [Observability](website/src/guides/observability.md) · [Upgrading projects](website/src/guides/upgrading.md) · [Offline profile](website/src/guides/offline.md) · [Security & production](website/src/guides/security.md) |
| **Reference** | [Environment variables](website/src/reference/environment.md) · [HTTP API](website/src/reference/http-api.md) · [api-policy.yaml](website/src/reference/api-policy-schema.md) · [Project manifest](website/src/reference/manifest.md) · [Exit codes](website/src/reference/exit-codes.md) · [Compared with google-agents-cli](website/src/reference/comparison.md) |

The CLI reference is generated from the commands themselves when the site is built; on the
command line, `graph-agents-cli <command> --help` shows the same. The bundled skills are
described in [skills/README.md](skills/README.md).

## Status

Version 0.2.0, **alpha**
([release notes](https://github.com/ss7172/graph-agents-cli/releases/tag/v0.2.0)).
Interfaces may still change between minor versions; [CHANGELOG.md](CHANGELOG.md) lists every
breaking change with its migration steps. Publication on PyPI is pending.

- [Known issues](KNOWN_ISSUES.md): parked issues, each with its impact and workaround.
- [Changelog](CHANGELOG.md): every release and its migration steps.
- [Contributing](CONTRIBUTING.md): development setup, tests, templates, releases and the
  upstream-sync process.

## License

Apache-2.0: see [LICENSE](LICENSE). graph-agents-cli is a fork of
[google-agents-cli](https://github.com/google/agents-cli) with the Google Cloud specific parts
removed; [NOTICE](NOTICE) lists the attribution and the modifications.
