<div align="center">

<img src="https://raw.githubusercontent.com/ss7172/graph-agents-cli/main/website/src/assets/logo.svg" alt="graph-agents-cli logo" width="96" height="96">

# graph-agents-cli

*Graphs in, agents out.*

**Build, evaluate and deploy [LangGraph](https://langchain-ai.github.io/langgraph/) agents on your own Kubernetes,<br>
with one CLI and six skills for your coding agent.**

[![PyPI](https://img.shields.io/pypi/v/graph-agents-cli)](https://pypi.org/project/graph-agents-cli/)
[![Python](https://img.shields.io/pypi/pyversions/graph-agents-cli)](https://pypi.org/project/graph-agents-cli/)
[![CI](https://img.shields.io/github/actions/workflow/status/ss7172/graph-agents-cli/ci.yml?branch=main&event=push&label=CI)](https://github.com/ss7172/graph-agents-cli/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/ss7172/graph-agents-cli/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-site-0f766e)](https://ss7172.github.io/graph-agents-cli/)
[![Skills tuned with SkillOpt](https://img.shields.io/badge/skills-tuned%20with%20SkillOpt-0f766e)](https://ss7172.github.io/graph-agents-cli/reference/skills-benchmark/)

[Get started](https://ss7172.github.io/graph-agents-cli/getting-started/) ·
[Guides](https://ss7172.github.io/graph-agents-cli/guides/) ·
[Reference](https://ss7172.github.io/graph-agents-cli/reference/) ·
[Skills benchmark](https://ss7172.github.io/graph-agents-cli/reference/skills-benchmark/) ·
[Changelog](https://github.com/ss7172/graph-agents-cli/blob/main/CHANGELOG.md) ·
[Issues](https://github.com/ss7172/graph-agents-cli/issues)

</div>

---

graph-agents-cli is a port of Google's [agents-cli](https://github.com/google/agents-cli) to
LangGraph on self-hosted Kubernetes: it keeps the lifecycle and the scaffold engine, and
replaces ADK on Google Cloud with LangGraph, Helm and any cluster
([how the two compare](https://ss7172.github.io/graph-agents-cli/reference/comparison/)).

**Works with your coding agent:** Claude Code · Codex · Gemini CLI · Cursor · Antigravity ·
*and others*. Or run each command yourself: the CLI works without a coding agent.

## What's new

- **2026-09-29 · 0.3.1 is on PyPI.** The first release published there:
  `uv tool install graph-agents-cli`
  ([release notes](https://github.com/ss7172/graph-agents-cli/releases/tag/v0.3.1)).
- **2026-09-29 · 0.3.0: agents calling agents.** Agents ask other agents over A2A for the
  user they serve, a person's approvals stay with that person, and `system` checks, wires and
  deploys several projects as one
  ([guide](https://ss7172.github.io/graph-agents-cli/guides/multi-agent/)). Also new:
  structured final answers in a JSON shape you declare, and skills tuned with Microsoft's
  [SkillOpt](https://github.com/microsoft/SkillOpt) ([results](https://ss7172.github.io/graph-agents-cli/reference/skills-benchmark/)).
  Measured while building it
  ([0.3.0 notes](https://github.com/ss7172/graph-agents-cli/blob/main/CHANGELOG.md#030---2026-09-29)):
  - wiring an agent to five other agents takes **5** `peer add` commands instead of 40 `api`
    commands and 418 hand-written lines;
  - replies that were not a bare JSON document (gpt-5-mini, 24-case triage task) went from
    28 of 48 to **0 of 96** with structured answers.

## What you get

| Feature | What it does |
|---|---|
| **Scaffold a real service** | `create` renders a LangGraph project: streaming chat API, A2A endpoint, eval harness, hardened Helm chart, GitHub Actions workflows. |
| **Run it locally, keyless** | `run` sends one prompt, `playground` serves a dev chat page; a deterministic fake model needs no key. |
| **An eval gate CI can enforce** | Deterministic checks and model judges; the exit code of `eval run` is the gate. |
| **Outbound calls under a policy** | `api-policy.yaml` declares the APIs tools may call; anything else is refused, and chosen calls wait for a human. |
| **Agents that ask other agents** | `peer add` declares the agents one asks over A2A; `system` checks, wires and deploys several projects as one. |
| **Secure by default** | One auth policy on every surface: a shared bearer key, OIDC/JWT or your own. |
| **Deploy to any cluster** | `deploy --env dev`, `staging` or `prod` with Helm, from your machine, GitHub Actions or Argo CD. |
| **Skills for your coding agent** | Six skills teach it the same lifecycle. All six are measured on a 104-task benchmark, and SkillOpt tuned three of them: workflow, scaffold and observability. |

Nothing is specific to one domain or one company: a project picks its auth policy, declares
its APIs and configures the rest through environment variables and chart values.

<!--
  DEMO VIDEO: pending the owner's approval of the video. Once approved, replace this comment
  with a "## Demo video" section, placed here after the overview as microsoft/SkillOpt's README
  does:
    - the GitHub user-attachments URL on a line of its own
      (https://github.com/user-attachments/assets/<id>), which GitHub renders as a player;
    - then a centered link:
      <p align="center"><a href="https://youtu.be/<id>"><b>Watch the full demo on YouTube</b></a></p>
  - optionally, a badge in the badge row:
      [![Demo video](https://img.shields.io/badge/demo-video-ff0000)](https://youtu.be/<id>)
  PyPI shows the user-attachments URL as plain text, so keep the YouTube link too.
  Absolute URLs only: this file is also the PyPI page.
-->

## Get started

You need Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
`setup` uses Node.js (`npx skills`) when it is there; deploying also needs `helm`, `kubectl`,
`git` and a `docker` that builds with BuildKit.

### 1. Install the CLI and the skills

```bash
uv tool install graph-agents-cli
graph-agents-cli setup      # optional: add the six skills to the coding agents it finds
```

`setup` also reinstalls the same release from its git tag
([Installation & setup](https://ss7172.github.io/graph-agents-cli/getting-started/installation/)
explains why).

<details>
<summary>Other ways to install: pipx, the release tag from GitHub, extras</summary>

```bash
pipx install graph-agents-cli
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.3.1   # the same release, from its git tag
uv tool install 'graph-agents-cli[a2a,langsmith]'                        # adds run --mode a2a and eval submit
```

Mirrors and disconnected installs:
[Installation & setup](https://ss7172.github.io/graph-agents-cli/getting-started/installation/).

</details>

### 2. Run your first agent, no model key needed

```bash
graph-agents-cli create my-agent && cd my-agent
cp .env.example .env
export MODEL_PROVIDER=fake            # the keyless, deterministic test model
graph-agents-cli login --write-env    # checks the setup, generates API_KEY
graph-agents-cli install
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli eval run             # the exit code is the gate
```

For a real model, leave out the `export` line and `login --write-env` asks for the key. The
provider is one setting: `openai`, `anthropic`, `gemini`, or `openai-compatible` for Ollama,
vLLM, TGI and others. The [Quickstart](https://ss7172.github.io/graph-agents-cli/getting-started/quickstart/)
walks through each step.

### 3. Build with your coding agent, then ship it

Once `setup` has installed the skills, open your coding agent in an empty directory and ask,
for example:

> *"Use graph-agents-cli to build an agent that answers questions about orders from our orders
> API (`GET /orders`) and can put an order on hold (`PATCH /orders/{order_id}`). Holding an
> order must wait for my approval. Use the fake model locally; when the evals pass, deploy it
> to my local kind cluster."*

The skills lead each step and stop for your decision at every gate
([coding-agent tutorial](https://ss7172.github.io/graph-agents-cli/getting-started/tutorial-coding-agent/)).
Or do it by hand: add an API tool under a policy, pass the `eval run` gate, then
`deploy --env dev` to a local kind cluster
([manual tutorial](https://ss7172.github.io/graph-agents-cli/getting-started/tutorial-manual/)).

## Skills, measured

`setup` installs six skills, all measured on gac-bench: 104 realistic graph-agents-cli tasks
with deterministic verifiers. SkillOpt proposed edits to the workflow, scaffold and
observability skills, and every edit was reviewed by hand before it shipped.

| Skill | What your coding agent learns |
|---|---|
| `graph-agents-cli-workflow` | The lifecycle (understand, scaffold, build, evaluate, deploy, observe), approval before deploy |
| `graph-agents-cli-langgraph-code` | LangGraph patterns the template uses: tools with `API_CALLS`, checkpointers, streaming, interrupts |
| `graph-agents-cli-scaffold` | `create`, `scaffold enhance`, `scaffold upgrade` and every flag |
| `graph-agents-cli-eval` | The eval gate, dataset schema, checks, judges and quality metrics |
| `graph-agents-cli-deploy` | Environments, rollouts and rollback, secrets, the Helm chart, Argo CD |
| `graph-agents-cli-observability` | Opt-in tracing (LangSmith or OTLP), JSON logs, `/metrics`, `/health` and `/ready` |

**0.3 skills against 0.2 skills on gac-bench** (hard pass rate, same CLI build, only the skill
text differs):

| Harness | Tasks | 0.2 skills | 0.3 skills | Change |
|---|---|---|---|---|
| Claude Code (claude-sonnet-5-5) | 104 | 0.84 | **0.97** | +0.12 (p = 0.0006) |
| Codex (gpt-5.6-terra) | 30 | 0.77 | **0.97** | +0.20 (p = 0.031) |

Codex ran the test split of all six skills plus val of workflow and observability. Change is
the mean of per-task differences, so it need not equal the difference of the two scores.

<details>
<summary>Where the gain is, per skill (Claude Code, val and test tasks)</summary>

| Skill | 0.2 skills | 0.3 skills | Change |
|---|---|---|---|
| `graph-agents-cli-workflow` | 0.36 | **1.00** | +0.64 |
| `graph-agents-cli-scaffold` | 0.61 | **1.00** | +0.39 |

The other four skills scored 0.92 or higher before and moved by 0.00 to +0.06.

</details>

No test task went down on either harness. There is one real regression on Claude Code (1 of 2
reps of a new deploy task in the val split), and the test-split gains alone are not significant (p = 0.25 and
0.5). Full tables:
[final-v0.3.md](https://github.com/ss7172/graph-agents-cli/blob/main/tools/skillopt/results/final-v0.3.md).

## Commands

| Command | What it does |
|---|---|
| `graph-agents-cli setup` | Install the CLI and the skills into detected coding agents |
| `graph-agents-cli create <name>` | Create a LangGraph agent project from a template |
| `graph-agents-cli run "prompt"` | Run the agent with a single prompt |
| `graph-agents-cli eval run` | Run the agent over the eval dataset and grade it |
| `graph-agents-cli deploy --env dev` | Deploy to the current Kubernetes context |

<details>
<summary>All commands</summary>

| Command | What it does |
|---|---|
| `login` · `info` · `install` | Check provider keys, LangSmith and kubeconfig (optionally write `.env`) · show configuration and version · install project dependencies |
| `create` · `scaffold` | Create a project · scaffold, enhance and upgrade projects |
| `run` · `playground` · `lint` | One prompt · the app locally with reload and a dev chat page · code checks plus the API-policy check |
| `api` · `approvals` · `auth` | Declare the outbound APIs tools may call · list and decide gated calls · dev-only JWTs |
| `peer` · `system` | Declare the agents this agent asks over A2A · check, wire and deploy agents that call each other |
| `eval` | Evaluate agents and compare results |
| `build` · `deploy` · `secrets` · `infra` | Build the image · deploy to Kubernetes · the app's Secret per environment · check prerequisites (read-only) |
| `setup` · `update` · `extension` | Install skills · force-reinstall them · manage extensions (experimental) |

`graph-agents-cli <command> --help` prints the same reference as the site's
[CLI page](https://ss7172.github.io/graph-agents-cli/reference/cli/).

</details>

## Documentation

| Section | Pages |
|---|---|
| **Get started** | [Installation](https://ss7172.github.io/graph-agents-cli/getting-started/installation/) · [Quickstart](https://ss7172.github.io/graph-agents-cli/getting-started/quickstart/) · [Build with a coding agent](https://ss7172.github.io/graph-agents-cli/getting-started/tutorial-coding-agent/) · [Manual workflow](https://ss7172.github.io/graph-agents-cli/getting-started/tutorial-manual/) · [The lifecycle](https://ss7172.github.io/graph-agents-cli/getting-started/lifecycle/) |
| **Build** | [Develop](https://ss7172.github.io/graph-agents-cli/guides/develop/) · [Authentication](https://ss7172.github.io/graph-agents-cli/guides/authentication/) · [API policy](https://ss7172.github.io/graph-agents-cli/guides/api-policy/) · [Human approval](https://ss7172.github.io/graph-agents-cli/guides/approvals/) · [Agents calling agents](https://ss7172.github.io/graph-agents-cli/guides/multi-agent/) · [Evaluation](https://ss7172.github.io/graph-agents-cli/guides/evaluation/) · [Extensions](https://ss7172.github.io/graph-agents-cli/guides/extensions/) |
| **Operate** | [Deploy](https://ss7172.github.io/graph-agents-cli/guides/deploy/) · [CI/CD](https://ss7172.github.io/graph-agents-cli/guides/cicd/) · [Secrets](https://ss7172.github.io/graph-agents-cli/guides/secrets/) · [Observability](https://ss7172.github.io/graph-agents-cli/guides/observability/) · [Upgrading](https://ss7172.github.io/graph-agents-cli/guides/upgrading/) · [Offline](https://ss7172.github.io/graph-agents-cli/guides/offline/) · [Security](https://ss7172.github.io/graph-agents-cli/guides/security/) |
| **Reference** | [CLI](https://ss7172.github.io/graph-agents-cli/reference/cli/) · [Environment](https://ss7172.github.io/graph-agents-cli/reference/environment/) · [HTTP API](https://ss7172.github.io/graph-agents-cli/reference/http-api/) · [api-policy.yaml](https://ss7172.github.io/graph-agents-cli/reference/api-policy-schema/) · [System file](https://ss7172.github.io/graph-agents-cli/reference/system-file/) · [Manifest](https://ss7172.github.io/graph-agents-cli/reference/manifest/) · [Exit codes](https://ss7172.github.io/graph-agents-cli/reference/exit-codes/) · [Skills](https://ss7172.github.io/graph-agents-cli/reference/skills/) · [Compared with google-agents-cli](https://ss7172.github.io/graph-agents-cli/reference/comparison/) |

## Status

Version 0.3.1, **alpha**, on [PyPI](https://pypi.org/project/graph-agents-cli/) and as tags on
GitHub ([release notes](https://github.com/ss7172/graph-agents-cli/releases/tag/v0.3.1)).
Interfaces may still change between minor versions; the changelog lists every breaking change
with its migration steps.

0.3's acceptance tests covered a system of six agents built with `peer add` and `system apply`
on a local cluster, security probes, a probe of 20 agents with 2 replicas each, an issuer that
hangs, and an upgrade from 0.2.0 with data. Blocker and major issues are fixed before a
release; the rest are parked.

- [Known issues](https://github.com/ss7172/graph-agents-cli/blob/main/KNOWN_ISSUES.md): parked issues, each with its impact and a workaround where one exists.
- [Changelog](https://github.com/ss7172/graph-agents-cli/blob/main/CHANGELOG.md): every release and its migration steps.
- [Contributing](https://github.com/ss7172/graph-agents-cli/blob/main/CONTRIBUTING.md): development setup, tests, templates, releases and the upstream-sync process.

## Credits

graph-agents-cli stands on two projects:

- **[google-agents-cli](https://github.com/google/agents-cli)**, Copyright 2026 Google LLC,
  Apache-2.0. graph-agents-cli is a fork of it, started from 1.6.1, with the Google Cloud
  specific parts removed; files kept from it keep their Google LLC copyright headers.
  [NOTICE](https://github.com/ss7172/graph-agents-cli/blob/main/NOTICE) lists the modifications.
- **[SkillOpt](https://github.com/microsoft/SkillOpt)** from Microsoft, which found the skill
  rules that 0.3 adopted after review.

## License

Apache-2.0: see [LICENSE](https://github.com/ss7172/graph-agents-cli/blob/main/LICENSE).
