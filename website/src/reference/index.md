---
description: Reference for graph-agents-cli commands, configuration, the generated service's HTTP API, skills and releases.
---

# Reference

<p class="gac-lede">Exact facts to look up: generated from the code where possible (the CLI
and the skills), included from the repository where they already live (known issues and the
changelog).</p>

## Commands and configuration

<div class="grid cards" markdown>

-   :material-console:{ .lg } **[CLI](cli.md)**

    Every command, subcommand and flag, generated from the CLI's own `--help`.

-   :material-variable:{ .lg } **[Environment variables](environment.md)**

    The CLI's variables and every setting of the generated service, with defaults.

-   :material-exit-to-app:{ .lg } **[Exit codes](exit-codes.md)**

    One scheme for every command: 0 ok, 1 refused or failed gate, 2 tool failure, 3
    configuration error.

-   :material-file-cog-outline:{ .lg } **[Project manifest](manifest.md)**

    `graph-agents-cli-manifest.yaml`: the project's settings, build record, secrets
    allow-list and environments.

-   :material-lan-connect:{ .lg } **[graph-agents-system.yaml](system-file.md)**

    Agent projects that call each other, for `graph-agents-cli system`: every key and what
    makes a file unusable.

</div>

## The generated service

<div class="grid cards" markdown>

-   :material-api:{ .lg } **[HTTP API](http-api.md)**

    `/chat` and its server-sent events, threads, approvals, health, readiness, metrics and A2A.

-   :material-file-document-check-outline:{ .lg } **[api-policy.yaml](api-policy-schema.md)**

    The outbound API policy schema: every key, the matching rules and the approval block.

</div>

## Skills and project history

<div class="grid cards" markdown>

-   :material-robot-outline:{ .lg } **[Skills](skills.md)**

    The six coding-agent skills `setup` installs, generated from the skills themselves.

-   :material-flask-outline:{ .lg } **[Skills benchmark](skills-benchmark.md)**

    For contributors: gac-bench and SkillOpt measure and improve the skills with Claude Code
    and Codex.

-   :material-compare-horizontal:{ .lg } **[Compared with google-agents-cli](comparison.md)**

    What this fork keeps, where it goes further and where it is behind.

-   :material-alert-circle-outline:{ .lg } **[Known issues](known-issues.md)**

    Parked medium- and low-priority issues, each with its impact and workaround.

-   :material-history:{ .lg } **[Changelog](changelog.md)**

    Every release, with breaking changes and their migration steps.

</div>

## Project resources

- [Contributing](https://github.com/ss7172/graph-agents-cli/blob/main/CONTRIBUTING.md):
  development setup, tests, templates, locks, releases and the upstream-sync process.
- [Skills source](https://github.com/ss7172/graph-agents-cli/tree/main/skills): the bundled
  skills and their references.
- [NOTICE](https://github.com/ss7172/graph-agents-cli/blob/main/NOTICE): attribution to
  google-agents-cli and the list of modifications.
- License: [Apache-2.0](https://github.com/ss7172/graph-agents-cli/blob/main/LICENSE).
