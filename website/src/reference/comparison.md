---
description: "How graph-agents-cli relates to google-agents-cli, the project it forked: what it keeps, where it goes further, where it is behind."
---

# Compared with google-agents-cli

<p class="gac-lede">graph-agents-cli is a fork of
<a href="https://github.com/google/agents-cli">google-agents-cli</a>. It keeps the lifecycle and
the scaffold engine, and targets LangGraph on any Kubernetes cluster instead of ADK on Google
Cloud.</p>

graph-agents-cli started from google-agents-cli 1.6.1, with the Google Cloud specific parts
removed and the agent framework, deployment target, evaluation backend and observability
replaced. [NOTICE](https://github.com/ss7172/graph-agents-cli/blob/main/NOTICE) lists the
modifications; files kept from the original project keep their Google LLC copyright headers.
This comparison is as of google-agents-cli 1.7.0 (September 2026).

## What it keeps

- **The lifecycle**: `setup`, `create` and `scaffold`, `run`, `eval`, `deploy`, extensions and
  a suite of coding-agent skills, in the same order and with the same shape.
- **The scaffold engine**: template layering, remote templates, and the 3-way merge behind
  `scaffold enhance` and `scaffold upgrade`.
- **The extension system**: command overrides and additions from git repositories or local
  paths.

## Side by side

| Area | graph-agents-cli | google-agents-cli |
|---|---|---|
| Agent framework | LangGraph (one Python template) | ADK (Python and other languages), plus a LangChain template |
| Where agents run | Any Kubernetes cluster, with Helm | Agent Runtime, Cloud Run, GKE |
| Continuous delivery | Direct `deploy`, a self-hosted runner (`helm-push`), or Argo CD through pull requests (`argocd`) | Cloud Build or GitHub Actions pipelines set up by `infra cicd` |
| Infrastructure | `infra check` reports prerequisites; creates nothing | `infra single-project` and `infra cicd` provision with Terraform |
| Authentication | In the app, on every surface: `shared-bearer`, `jwt` (OIDC) or a `custom` policy | Google Cloud's identity layer |
| Outbound calls | `api-policy.yaml` in the project, enforced at runtime and checked by `lint`, with human approval of chosen calls | An Agent Gateway bound at deploy time (Agent Runtime) |
| Evaluation | Local deterministic checks and model judges, an enforceable gate, a keyless fake model | Agent Platform evaluation, dataset synthesis, user simulation, prompt optimisation |
| Secrets | `secrets apply` and `secrets status` for an allow-listed Kubernetes Secret | Secret Manager |
| Observability | LangSmith or OpenTelemetry, opt-in, metadata-only by default | Cloud Trace, logging, BigQuery Agent Analytics |
| Publishing | Out of scope | Gemini Enterprise, Agent Registry |
| Distribution | PyPI, and a pinned git tag per release | PyPI |

## Where it goes further

- **An enforceable eval gate**: every planned case accounted for, deterministic checks and
  mandatory judges with no threshold, quality metrics with an explicit `min_pass_rate`, exit
  codes CI can use, `eval compare --fail-on-regression`, and a deterministic fake model for
  keyless CI. See [Evaluation](../guides/evaluation.md).
- **An outbound API policy** enforced at runtime and checked by `lint`, with
  [human approval](../guides/approvals.md) of the calls a prompt injection could abuse. See
  [Outbound API policy](../guides/api-policy.md).
- **Kubernetes Secrets management** (`secrets apply` and `status`, allow-listed keys,
  pre-deploy checks) and Argo CD GitOps through pull requests.
- **A disconnected profile**, verified by `login` and `infra check`. See
  [Offline profile](../guides/offline.md).
- **Self-hosted auth policies** (shared bearer, OIDC/JWT, custom) enforced in the app on every
  surface, instead of a cloud provider's identity layer.

## Where it is behind

- **Evaluation**: no prompt optimisation, dataset synthesis, user simulation or results fetch
  (`eval optimize`, `eval dataset synthesize`, `eval results` upstream); eval cases are written
  by hand.
- **Infrastructure**: no provisioning. `infra check` only reports; upstream's `infra cicd`
  creates the CI/CD setup with Terraform.
- **Templates and languages**: one Python LangGraph template and no sample catalogue, against
  upstream's ADK templates in several languages, samples and a LangChain template.
- **Lint**: no type checker or spell checker in the generated project's `lint`.
- **Maturity**: upstream has a long release history. This project has four tagged releases
  (`v0.1.0`, `v0.2.0`, `v0.3.0`, `v0.3.1`), this documentation site (published once the
  maintainers enable it) and the skills; it is on PyPI from 0.3.1.
- **Upstream fixes are ported by hand** after 1.6.1, following the upstream-sync process in
  [CONTRIBUTING.md](https://github.com/ss7172/graph-agents-cli/blob/main/CONTRIBUTING.md#upstream-sync).
  For example, remote templates still skip symlinks, which upstream 1.7.0 copies when they
  stay inside the repository.

Each gap is tracked in [Known issues](known-issues.md), for example
[KI-093](known-issues.md#ki-093-remote-templates-skip-symlinks-upstream-fixes-are-ported-by-hand).

## Out of scope

Google Cloud targets (Agent Runtime, Cloud Run, GKE-specific integrations), publishing to
Gemini Enterprise and BigQuery analytics are out of scope, not gaps.

<div class="grid cards gac-cols-3" markdown>

-   :material-home-outline:{ .lg } **[Home](../index.md)**

    What graph-agents-cli does, in one page.

-   :material-alert-circle-outline:{ .lg } **[Known issues](known-issues.md)**

    Parked issues, each with its impact and workaround.

-   :material-history:{ .lg } **[Changelog](changelog.md)**

    Every release and its migration steps.

</div>
