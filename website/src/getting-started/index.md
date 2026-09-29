---
description: Install graph-agents-cli, run a LangGraph agent locally in five minutes, then build one end to end.
---

# Get started

<p class="gac-lede">Install the CLI, run an agent on your machine without a model key, then
build a real one, either by asking your coding agent or command by command.</p>

You need Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Everything up to deployment runs locally; a Kubernetes cluster is needed only to deploy.

<div class="grid cards gac-steps" markdown>

-   **Installation & setup**

    Install the CLI from PyPI, add the skills to your coding agents and check
    your environment with `login`.

    [Install graph-agents-cli](installation.md)

-   **Quickstart**

    Five minutes: create a project, ask it a question and run the eval gate on the
    deterministic fake model, then switch to a real provider.

    [Run your first agent](quickstart.md)

-   **Pick a tutorial**

    Build an agent that calls an API with a policy and an approval, evaluate it and deploy
    it to a local cluster, with your coding agent or by hand.

    [Build with a coding agent](tutorial-coding-agent.md) ·
    [Manual workflow](tutorial-manual.md)

</div>

## Understand the lifecycle

Every project follows the same path: create, develop, evaluate, deploy, operate.
[The lifecycle](lifecycle.md) explains each stage, the commands that belong to it and what
their exit codes mean.

<div class="grid cards gac-cols-3" markdown>

-   :material-sync:{ .lg } **[The lifecycle](lifecycle.md)**

    The stages, the commands in each, and how a change moves from your laptop to production.

-   :material-book-open-variant:{ .lg } **[Guides](../guides/index.md)**

    Task-by-task depth once the basics work: auth, the API policy, approvals, deployment.

-   :material-console:{ .lg } **[CLI reference](../reference/cli.md)**

    Every command and flag, generated from the CLI itself.

</div>
