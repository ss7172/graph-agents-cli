---
description: Task guides for building, securing, evaluating, deploying and operating graph-agents-cli projects.
---

# Guides

<p class="gac-lede">One task per page. The build guides cover the agent and its guardrails;
the operate guides take it to a cluster and keep it healthy.</p>

## Build

<div class="grid cards" markdown>

-   :material-code-braces:{ .lg } **[Develop your agent](develop.md)**

    The generated project, the service and its endpoints, tools and prompts, answers in a
    JSON shape you declare, the `fastapi` and `langgraph-server` runtimes, `run` and
    `playground`.

-   :material-account-key-outline:{ .lg } **[Authentication](authentication.md)**

    One policy on every surface: `shared-bearer`, per-user `jwt` (with local dev tokens) or
    a `custom` policy of your own.

-   :material-shield-check-outline:{ .lg } **[Outbound API policy](api-policy.md)**

    Declare the APIs tools may call in `api-policy.yaml`, widen or narrow access with
    `graph-agents-cli api`, and let `lint` check every declared call.

-   :material-account-check-outline:{ .lg } **[Human approval](approvals.md)**

    Make chosen calls wait for the requester or a second person, decide them with
    `approvals`, and understand what an approval binds.

-   :material-lan-connect:{ .lg } **[Agents calling agents](multi-agent.md)**

    Let an agent ask other agents for its user: `peer add`, token exchange, relayed
    approvals, the system view for many projects, and the threat model.

-   :material-check-decagram-outline:{ .lg } **[Evaluation](evaluation.md)**

    Datasets, deterministic checks, judges and quality metrics, and the gate that `eval run`
    enforces locally and in CI.

-   :material-puzzle-outline:{ .lg } **[Extensions](extensions.md)**

    Override or add CLI commands from an extension repository or a local path (experimental).

</div>

## Operate

<div class="grid cards" markdown>

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    Environments, the Helm chart, local clusters, direct deploys, rollouts, rollback and an
    external database.

-   :material-source-pull:{ .lg } **[CI/CD](cicd.md)**

    The `skip`, `helm-push` and `argocd` modes, the generated workflows and the GitHub
    settings they need.

-   :material-key-chain-variant:{ .lg } **[Secrets](secrets.md)**

    The allow-listed app Secret: apply, check and rotate keys without printing a value.

-   :material-chart-timeline-variant:{ .lg } **[Observability](observability.md)**

    JSON logs, Prometheus metrics, run records and opt-in tracing with LangSmith or OTLP.

-   :material-update:{ .lg } **[Upgrading projects](upgrading.md)**

    `scaffold upgrade` with a 3-way merge against the build that created the project, and
    migrating from 0.1.0.

-   :material-lan-disconnect:{ .lg } **[Offline profile](offline.md)**

    Run the whole lifecycle without internet access: on-network models, mirrors and checks.

-   :material-shield-lock-outline:{ .lg } **[Security & production](security.md)**

    The security model, what it leaves to you, and the checklist before production traffic.

</div>
