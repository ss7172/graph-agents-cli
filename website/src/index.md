---
title: graph-agents-cli
description: A CLI and coding-agent skills for building, evaluating and deploying LangGraph agents on your own Kubernetes cluster.
template: home.html
hide:
  - navigation
  - toc
---

<div class="gac-hero" markdown>

<div class="gac-hero__copy" markdown>

<p class="gac-eyebrow">Graphs in, agents out.</p>

# Build, evaluate and deploy <span class="gac-hl">LangGraph agents</span> on your own Kubernetes

<p class="gac-lede">One CLI, and six skills for your coding agent, take an agent from
<code>create</code> to a hardened Helm release: a streaming chat API, shared-key or per-user
auth, an outbound API policy, human approval of risky calls and an eval gate CI can enforce.</p>

[Get started](getting-started/index.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/ss7172/graph-agents-cli){ .md-button }

<div class="gac-works">
  <span class="gac-works__label">Works with your coding agent</span>
  <span class="gac-pill">Claude Code</span>
  <span class="gac-pill">Codex</span>
  <span class="gac-pill">Gemini CLI</span>
  <span class="gac-pill">Cursor</span>
  <span class="gac-pill">Antigravity</span>
  <span class="gac-works__more">and more</span>
</div>

</div>

<div class="gac-terminal" markdown>

```bash
# install the CLI from its release tag, then the skills
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0
graph-agents-cli setup

# create an agent and ask it a question: the fake model needs no key
graph-agents-cli create my-agent && cd my-agent
cp .env.example .env
export MODEL_PROVIDER=fake
graph-agents-cli login --write-env
graph-agents-cli install
graph-agents-cli run "What's the weather in San Francisco?"
```

</div>

</div>

<div class="gac-section" markdown>

## Get started in three steps

Everything runs on your machine first; a model key and a cluster come later.

<div class="grid cards gac-steps" markdown>

-   **Install the CLI and the skills**

    One `uv tool install` from the release tag, then `setup` adds the skills to the coding
    agents it finds.

    [Installation & setup](getting-started/installation.md)

-   **Create and run an agent**

    `create` a project, let `login --write-env` fill in `.env`, `install`, then `run` a
    prompt. Five minutes on the fake model, no key.

    [Quickstart](getting-started/quickstart.md)

-   **Evaluate and deploy it**

    Add an API tool under a policy, pass the `eval run` gate, then `deploy --env dev` to a
    local kind cluster.

    [Tutorial: manual workflow](getting-started/tutorial-manual.md)

</div>

</div>

<div class="gac-section" markdown>

## What you get

A generic toolkit: nothing in the CLI or the generated project is specific to one domain or one company.

<div class="grid cards" markdown>

-   :material-folder-plus-outline:{ .lg } **Scaffold a real service**

    `create` renders a LangGraph project with a streaming chat API, an A2A endpoint, an
    eval harness, a hardened Helm chart and GitHub Actions workflows.

    [Develop your agent](guides/develop.md)

-   :material-play-circle-outline:{ .lg } **Run it locally**

    `run` sends a prompt through a temporary local server; `playground` serves a dev chat
    page with reload. A deterministic fake model needs no key.

    [Quickstart](getting-started/quickstart.md)

-   :material-check-decagram-outline:{ .lg } **Evaluate with a gate**

    `eval run` sends every dataset case to the agent, grades deterministic checks and
    LLM judges, and its exit code is the gate your CI enforces.

    [Evaluation](guides/evaluation.md)

-   :material-kubernetes:{ .lg } **Deploy to any Kubernetes**

    `deploy --env dev|staging|prod` with Helm, directly or through Argo CD pull requests.
    Local clusters (kind, k3d, minikube, Docker Desktop) need no registry push: any valid
    name, such as `--registry localhost/dev`, works.

    [Deploy to Kubernetes](guides/deploy.md)

-   :material-shield-lock-outline:{ .lg } **Secure by default**

    One auth policy on every surface (shared bearer, OIDC/JWT or your own), an outbound
    API allow-list and human approval of the calls you choose.

    [Security & production](guides/security.md)

-   :material-robot-outline:{ .lg } **Built for coding agents**

    Six bundled skills teach your coding agent the same lifecycle, so you can ask it to
    "use graph-agents-cli to build ..." and review each step.

    [Build with a coding agent](getting-started/tutorial-coding-agent.md)

</div>

</div>

<div class="gac-section" markdown>

## One lifecycle, from prototype to production

Each stage is a command, and each command's exit code tells a script or a coding agent what happened.

<ol class="gac-lifecycle" markdown="block">

<li markdown="block">

[Create](getting-started/quickstart.md)
{: .gac-lifecycle__stage }

A project with its API, auth, policy, chart and CI.

`create` `scaffold enhance`

</li>

<li markdown="block">

[Develop](guides/develop.md)
{: .gac-lifecycle__stage }

Write tools, declare the APIs they call, try it.

`run` `playground` `api` `lint`

</li>

<li markdown="block">

[Evaluate](guides/evaluation.md)
{: .gac-lifecycle__stage }

Grade every case; the exit code is the gate.

`eval run` `eval compare`

</li>

<li markdown="block">

[Deploy](guides/deploy.md)
{: .gac-lifecycle__stage }

Build, apply the Secret, roll out with Helm or Argo CD.

`build` `secrets apply` `deploy`

</li>

<li markdown="block">

[Operate](guides/observability.md)
{: .gac-lifecycle__stage }

Watch rollouts, decide approvals, upgrade the project.

`deploy --status` `approvals` `scaffold upgrade`

</li>

</ol>

<p class="gac-lifecycle__loop" markdown>Every change goes round again: develop, evaluate, deploy.
[The lifecycle](getting-started/lifecycle.md) explains each stage.</p>

</div>

<div class="gac-section" markdown>

## Where to next

<div class="grid cards" markdown>

-   :material-rocket-launch-outline:{ .lg } **Get started**

    Install, run your first agent in five minutes, then follow a tutorial.

    [Get started](getting-started/index.md)

-   :material-book-open-variant:{ .lg } **Guides**

    Authentication, the API policy, approvals, evaluation, deployment, secrets and more.

    [Guides](guides/index.md)

-   :material-console:{ .lg } **Reference**

    Every command and flag, environment variables, the HTTP API, exit codes and skills.

    [Reference](reference/index.md)

</div>

</div>
