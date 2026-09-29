---
description: The exit-code scheme every graph-agents-cli command follows, with the cases each command maps to it.
---

# Exit codes

<p class="gac-lede">One exit-code scheme for every command, so scripts, CI jobs and coding
agents can act on a result without parsing its output.</p>

## The scheme

| Code | Meaning | Act on it by |
|---|---|---|
| `0` | Success. For a gate (`eval`, `secrets status`), the gate is met. | Carrying on. |
| `1` | Refused by a policy or a mode, a declined confirmation, or a failed gate. | Reading the message: the fix is a change to the project or a decision. |
| `2` | A tool failed or could not be reached: `helm`, `kubectl`, `docker`, `git`, `gh`, `uvx`, a local server, the agent. Also an incomplete eval run, a Click usage error and an unexpected crash. | Retrying once the tool or network works, or reporting a bug. |
| `3` | Configuration error: not in a project, an invalid manifest, env file, policy, response schema, port or kube context, a placeholder left in place. | Fixing the configuration the message names. |

The scheme is the same everywhere, and this page maps every command to it. The gate and run
commands (`run`, `lint`, `login`, `auth dev-token`, `eval run`, `eval generate`, `eval grade`,
`secrets status`, `scaffold enhance`) and the `api`, `approvals` and `eval` groups also list
their cases in `--help` (rendered in the [CLI reference](cli.md)).

## Beyond 0 to 3

| Situation | Exit code |
|---|---|
| A usage error from Click (an unknown flag, a missing argument) | `2` |
| A prompt answered "no", or Ctrl-D at a prompt (`Aborted!`) | `1` |
| Ctrl-C | `130` |
| `SIGTERM` (a CI timeout, an IDE stop button, `kill`) | `143` |
| `SIGHUP` | `129` |

A signal ends a command with `128 + N` only after the local server it started (`run`,
`eval generate`, `playground`) has been stopped and its pid file removed, so an interrupted
run leaves no server behind.

A network, file-system or parse problem prints one line instead of a traceback: invalid YAML
or JSON is `3`, and so is a proxy setting the CLI cannot use (see
[Environment variables](environment.md#read-by-the-tools-the-cli-runs)); a connection error, a
timeout or an OS error is `2`. Set
`GRAPH_AGENTS_CLI_DEBUG=1` to see the traceback behind it. Anything else unexpected is a bug:
the traceback is printed and the command exits `2`.

## Per command

=== "Develop"

    | Command | `1` | `2` | `3` |
    |---|---|---|---|
    | [`run`](cli.md#graph-agents-cli-run) | The agent answered with an error (an HTTP error or an `error` event), or the server refused a decision (not an approver, already decided, expired) | The agent could not be reached or went silent; the local server did not start, or `--stop-server` could not stop it (the processes still running are named and its record is kept) | No project; the port is unavailable |
    | [`approvals`](cli.md#graph-agents-cli-approvals) | The server refused (not an approver, already decided, expired, not found), or the resumed run ended with an error | The agent could not be reached | Not in a project and no `--url` |
    | [`lint`](cli.md#graph-agents-cli-lint) | A refused call or an unreadable `API_CALLS`, or ruff failed | | An invalid `api-policy.yaml` or response schema (`app/response_schema.json`), the retired product policy, or not in a project |
    | [`api`](cli.md#graph-agents-cli-api) | `api check`: a declared call is refused | Usage error | An invalid result (nothing is written), an invalid `api-policy.yaml`, or not in a project |
    | [`install`](cli.md#graph-agents-cli-install) | `uv sync` failed, or `uv` is missing | | Not in a project |
    | [`login`](cli.md#graph-agents-cli-login) | A check failed (`0` with `--status`) | | |
    | [`auth dev-token`](cli.md#graph-agents-cli-auth-dev-token) | | The project's environment could not sign (run `install`) | Not a `jwt` project under `APP_ENV=dev`, or another key or issuer is configured |

    `run` exits `0` when a run is left waiting for an approval: the pause is a normal outcome,
    and the output prints the `approvals` commands that decide it.

=== "Evaluate"

    | Command | `1` | `2` | `3` |
    |---|---|---|---|
    | [`eval run`](cli.md#graph-agents-cli-eval-run) | The gate failed | A case is `error` or `missing` (an incomplete run) | No dataset, a malformed case, an unknown metric, an unreachable judge, a metric without a threshold, no project |
    | [`eval generate`](cli.md#graph-agents-cli-eval-generate) | | A case is `error` or `missing` | No dataset, a malformed case, no project |
    | [`eval grade`](cli.md#graph-agents-cli-eval-grade) | A case failed, or a quality metric is under its `min_pass_rate` | A case is `error` or `missing` | Unknown metric, unreachable judge, no threshold |
    | [`eval compare`](cli.md#graph-agents-cli-eval-compare) | `--fail-on-regression` and a regression | | An unreadable results file |
    | [`eval submit`](cli.md#graph-agents-cli-eval-submit) | | LangSmith rejected or never received the upload | LangSmith is not configured |

    The exit code of `eval run` is the gate: the worse of its two stages. See
    [Evaluation](../guides/evaluation.md) for what the gate checks.

=== "Deploy and operate"

    | Command | `1` | `2` | `3` |
    |---|---|---|---|
    | [`build`](cli.md#graph-agents-cli-build) | `docker` is missing | `docker` failed | A placeholder (`ghcr.io/CHANGE-ME`) or invalid registry |
    | [`deploy`](cli.md#graph-agents-cli-deploy) | Refused by the CD mode or by policy, a declined context confirmation, a required Secret key missing (or, outside `dev`, the Secret itself when the env file sets no allow-listed key), `--status` not complete within `--timeout` | `helm`, `kubectl`, `docker`, `git` or `gh` failed or is missing; another helm operation holds the release; `--restart` pods not ready | No env file for the environment, a placeholder registry, a `CHANGE-ME` left in the chart `env`, incomplete `jwt` settings or an agent it could not call outside dev, an unknown context |
    | [`system check`](cli.md#graph-agents-cli-system-check) | A finding with an error (SC01-SC15) | | The system file is unreadable or invalid, names an unknown project or an ambiguous edge |
    | [`system apply`](cli.md#graph-agents-cli-system-apply) | | Usage error (an environment not in the file) | The system file cannot be used, or a project cannot take its edges (nothing is written) |
    | [`system deploy`](cli.md#graph-agents-cli-system-deploy) | The static check found an error (nothing is deployed), a deploy failed, or the live check found an error | Usage error (`--only` names no agent, a local environment) | The system file cannot be used; outside `dev`, a project records no kube context |
    | [`secrets apply`](cli.md#graph-agents-cli-secrets-apply) | Refused (a declined confirmation) | `kubectl` failed | No env file, unknown environment or context |
    | [`secrets status`](cli.md#graph-agents-cli-secrets-status) | The Secret is missing, or a required key is (any key with `--strict`) | `kubectl` failed (unreachable cluster, credentials, RBAC) | Unknown environment or context, no manifest |
    | [`infra check`](cli.md#graph-agents-cli-infra-check) | A required item is missing | | No manifest |

    `secrets status` is meant as a pre-deploy gate: it exits `0` only when the Secret holds
    every required key. See [Deploy to Kubernetes](../guides/deploy.md) and
    [Secrets](../guides/secrets.md).

=== "Project and CLI"

    | Command | `1` | `2` | `3` |
    |---|---|---|---|
    | [`scaffold enhance`](cli.md#graph-agents-cli-scaffold-enhance) | Applied, but steps marked `(required)` are left for you | Usage error (a model of another provider); version-locked enhance: `uvx` is missing or could not run the prior build | No project for `--dry-run`, a legacy policy file |
    | [`scaffold upgrade`](cli.md#graph-agents-cli-scaffold-upgrade) | | The baseline build could not be fetched or run (`uvx` missing, no network); nothing is changed | A missing or unreleased `cli_version`, a recorded build that cannot be rebuilt, a `--baseline-ref` that names no build or a build of another version, an unusable `GRAPH_AGENTS_CLI_INSTALL_SPEC` |
    | [`extension add`](cli.md#graph-agents-cli-extension-add) | A declined trust prompt | git or the network failed to resolve a repository | A local path that is not an extension |
    | [`setup`](cli.md#graph-agents-cli-setup), [`update`](cli.md#graph-agents-cli-update) | | | An unusable `GRAPH_AGENTS_CLI_INSTALL_SPEC` |

    `setup` and `update` treat installing the CLI as best effort: when `uv tool install`
    fails they print the command to run by hand and carry on with the skills. See
    [Upgrading projects](../guides/upgrading.md) for the upgrade baseline.

## In a script

Branch on the code, not on the output:

```bash
graph-agents-cli eval run
case $? in
  0) echo "gate met" ;;
  1) echo "gate failed: fix the agent or the cases"; exit 1 ;;
  2) echo "incomplete run: the agent or a tool was unreachable"; exit 1 ;;
  3) echo "configuration error"; exit 1 ;;
esac
```

A few real exits, from a scratch directory and a fresh project on the fake model:

```console
$ graph-agents-cli lint                      # outside a project
Error: No graph-agents-cli-manifest.yaml found in the current directory or its parents.
$ echo $?
3
$ graph-agents-cli run --bogus
Error: No such option '--bogus'. Did you mean '--verbose'?
$ echo $?
2
$ graph-agents-cli eval run                  # in a project, MODEL_PROVIDER=fake
Result: gate met (exit code 0) (fake model: plumbing check only, not a quality signal)
$ echo $?
0
```

<div class="grid cards gac-cols-3" markdown>

-   :material-console:{ .lg } **[CLI reference](cli.md)**

    Every command and flag, as `--help` prints them.

-   :material-check-decagram-outline:{ .lg } **[Evaluation](../guides/evaluation.md)**

    What the eval gate checks, and why CI can trust its exit code.

-   :material-sync:{ .lg } **[The lifecycle](../getting-started/lifecycle.md)**

    Where each command sits between create and operate.

</div>
