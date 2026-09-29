---
description: Upgrade a graph-agents-cli project to a newer CLI build with scaffold upgrade, keep your edits, and migrate a 0.1.0 project or a running deployment.
---

# Upgrading projects

<p class="gac-lede">A new CLI release brings new templates. <code>scaffold upgrade</code> merges
them into your project with a 3-way merge against the templates of the exact build that
created it, so your edits are kept and only the files you never touched are replaced.</p>

## Upgrade or enhance?

| You want to | Use | What it does |
|---|---|---|
| Move the project to a newer CLI version | [`scaffold upgrade`](../reference/cli.md#graph-agents-cli-scaffold-upgrade) | Merges the template changes between the build that created the project and the running build. |
| Change a setting: CD mode, runtime, model provider, registry, deployment target | [`scaffold enhance`](../reference/cli.md#graph-agents-cli-scaffold-enhance) | Re-renders the scaffolding for the new settings, at the project's own version. |

## Upgrade a project

1. **Update the CLI** (and the skills with it):

    ```bash
    graph-agents-cli update
    graph-agents-cli --version
    ```

2. **Preview the upgrade.** Nothing is written:

    ```bash
    graph-agents-cli scaffold upgrade --dry-run
    ```

3. **Apply it.** `-y` applies every non-conflicting change; `-i` also walks you through each
   conflict. A backup is written first.

    ```bash
    graph-agents-cli scaffold upgrade -y
    ```

4. **Check the result:**

    ```bash
    graph-agents-cli install
    MODEL_PROVIDER=fake uv run pytest tests/unit tests/integration
    graph-agents-cli lint
    ```

5. **Read the release's migration notes** in the [changelog](../reference/changelog.md) before
   you deploy, and follow [Upgrade a running deployment](#upgrade-a-running-deployment).

A project that is up to date says so:

```console
$ graph-agents-cli scaffold upgrade --dry-run
✅ Project is already at version 0.2.0 (build 0.2.0+g181aebc)
```

## How the merge works

`scaffold upgrade` re-renders the project's settings twice: with the build that created it (the
*baseline*, run through `uvx`) and with the running build. It compares both with your files:

| Files | What upgrade does |
|---|---|
| **Agent code:** `app/agent.py`, `app/tools/**`, `app/policies/**`, `app/prompts/**`, `app/graph/**` | Never modified. |
| **Configuration:** `.env`, `.env.*`, `api-policy.yaml`, `deployment/helm/<name>/values-*.yaml`, `deployment/argocd/**`, `tests/eval/datasets/**`, `tests/eval/eval_config.yaml` | Never overwritten. |
| **Dependencies:** `pyproject.toml`, the manifest | Merged semantically. |
| **Scaffolding:** everything else (`values.yaml`, chart templates, `app/app_utils/**`, `app/fast_api_app.py`, the Dockerfile, the workflows) | Replaced when you did not edit it; a conflict when both you and the template changed it. |
| Files you added, in neither template | Left alone. |

Conflicts are listed, or resolved one by one with `-i`. The backup goes to
`~/.graph-agents-cli/backups/` (private: 0700, `.env*` files 0600), which keeps the newest five
backups of each project.

!!! warning "Check `values.yaml` after an upgrade"
    A chart `values.yaml` that you and the template both changed is kept whole, so settings the
    new template adds (the shutdown drain, for example) are silently missing
    ([KI-040](../reference/known-issues.md#ki-040-scaffold-upgrade-keeps-an-edited-chart-valuesyaml-whole-dropping-new-settings)).
    Compare it with a fresh `create` of the same settings, merge by hand and check the result
    with `helm template`. Your `values-<env>.yaml` files are never touched.

## Which build created the project

The manifest names the baseline:

```yaml title="graph-agents-cli-manifest.yaml"
cli_version: '0.2.0'
# The build that rendered this project and a digest of it; `scaffold upgrade` reads both
cli_build:
  id: 0.2.0+g181aebc
  commit: 181aebc93fcc537ebb87420128f354eb9a3f3dd0
  template_digest: sha256:e2d79261cfe8a6dde6482fee880c787e7582ca89a48e2c19b2b4c839372a7c50
generated_at: '2026-09-25T02:51:13.329087+00:00'
```

- `cli_version` is the release.
- `cli_build` is written by `create`, `enhance` and `upgrade`: the build id that
  `graph-agents-cli --version` prints (`0.2.0` for the release, `0.2.0+g<commit>` for a build
  between releases, `.dirty` added for uncommitted changes), its full commit, and
  `template_digest`, a digest of what that build renders for the project's settings.
- `generated_at` is when the project was created.

`graph-agents-cli info` shows it as `Scaffolded with: 0.2.0 (build ...)`. From these fields:

| The manifest records | The baseline is |
|---|---|
| an older release | the release tag `v<cli_version>`, or the recorded commit when `cli_build` names a build between releases |
| the running version, the same build or the same `template_digest` | none: "already at version" |
| the running version, another build | the recorded commit |
| no `cli_build` (a project made before builds were recorded, for example by a pre-release 0.2.0 build) | compared by version only: "already at version", with how to name the build |

After an upgrade the manifest records the running build, so the next upgrade needs no flag.
Every release is tagged `v<version>` on the repository (`v0.1.0`, `v0.2.0`), and that tag is
the default baseline for the projects the release created.

## Name the baseline with `--baseline-ref`

`--baseline-ref REF` names the build that created the project and wins over the manifest:

| `REF` | Example |
|---|---|
| a commit or tag of the repository | `1a2b3c4`, `v0.1.0` |
| `<clone>@<commit>`: a commit in a local clone, looked up there first (the way to reach a commit that was never pushed) | `~/src/graph-agents-cli@1a2b3c4` |
| a path to a checkout or a wheel, rebuilt, never a stale uv cache | `~/src/graph-agents-cli` |
| a full install spec, for a mirror | `git+https://git.example.com/graph-agents-cli@1a2b3c4` |

The baseline must render the manifest's `cli_version`; a build of another version is exit 3.
Without a known commit, the newest one before the project was generated is a first candidate:

```bash
git -C <clone> log -1 --format=%H --before='<generated_at from the manifest>'
graph-agents-cli scaffold upgrade --baseline-ref <clone>@<commit> --dry-run
graph-agents-cli scaffold upgrade --baseline-ref <clone>@<commit> -y
```

The build may be older than that commit (a checkout behind its branch, or a stale uv build), so
always read the dry run first. With the right build, only files you edited are listed under
"Will preserve" or as conflicts. Many scaffolding files you never touched there mean a later
build than the one that created the project; `upgrade` warns when most template files would
keep their current content. Applied with `-y` anyway, a wrong baseline records the project as
up to date while its files stay old
([KI-041](../reference/known-issues.md#ki-041-a-wrong-baseline-ref-applied-with-y-records-a-stale-project-as-up-to-date));
run again with the right `--baseline-ref` to recover.

A build between releases has no tag: `upgrade` rebuilds it from the commit `cli_build` records,
fetched from the repository. A commit that was never pushed is named with
`--baseline-ref <clone>@<commit>`. A build with uncommitted changes (`.dirty`) cannot be
rebuilt at all: name the closest commit with `--baseline-ref`.

### `--baseline current`

`--baseline current` compares against the current templates instead of the build that created
the project. It is an explicit, logged opt-in, and the summary is labelled as such. It cannot
tell your edits from the template changes since the old version: every file listed under "Will
preserve" that you did not edit keeps its old content, and dependency changes are not merged.
Use it only knowingly, and never for a 0.1.0 project.

## Install the baseline from a mirror

`setup`, `update`, the upgrade baseline and generated projects' CI install the CLI from a pinned
git tag of the repository. `GRAPH_AGENTS_CLI_INSTALL_SPEC` points them at a private mirror, a
wheel or a package index. Write `{version}` where the version goes, so the baseline can install
an older release:

```bash
export GRAPH_AGENTS_CLI_INSTALL_SPEC='git+https://git.example.com/graph-agents-cli@v{version}'
graph-agents-cli scaffold upgrade --dry-run
```

- An override without `{version}` is refused for the baseline (exit 3): it would install one
  fixed build in place of the old version.
- `{version}` is a release number (the `cli_version` a project records), so the override names
  releases only. Name a build between releases with `--baseline-ref`.
- An override with control characters or whitespace is refused (exit 3), except the spaces of
  a PEP 508 `name @ url` reference.
- At `create` time the override also becomes the new project's `GRAPH_AGENTS_CLI_SPEC` in
  `.github/agent.env`.

## Exit codes

| Exit | `scaffold upgrade` |
|---|---|
| 0 | upgraded, previewed, or already at version |
| 2 | the baseline could not be built: `uvx` is missing, or could not fetch and run the old build (no tag on the remote, a commit never pushed, no network). Nothing was changed. |
| 3 | not in a project; the manifest's `cli_version` is missing or not a release; the recorded build cannot be rebuilt (`.dirty`, or a build between releases while an install-spec override is set); `--baseline-ref` names no build or a build of another version; an unusable `GRAPH_AGENTS_CLI_INSTALL_SPEC` |

See [Exit codes](../reference/exit-codes.md) for every command.

## Change settings with `scaffold enhance`

`scaffold enhance` adds or changes the deployment target, CD mode, runtime, model provider or
registry of an existing project, with the same 3-way merge after a backup:

```bash
graph-agents-cli scaffold enhance --cd argocd --dry-run
graph-agents-cli scaffold enhance --runtime langgraph-server
graph-agents-cli install        # after --runtime: updates uv.lock
```

- It works at the project's own version: when the manifest names another version, it runs that
  version's CLI through `uvx` (exit 2 without `uvx`).
- A runtime or model-provider change also updates the model default, `secrets.keys`,
  `.env.example`, the chart values (key by key, around your edits) and `.github/agent.env`.
  What it cannot apply is listed under "Left for you"; steps marked `(required)` make it exit 1,
  and an edited Dockerfile gets the new version beside it as `Dockerfile.new`.
- It never touches `api-policy.yaml`: change the policy with `graph-agents-cli api`.
- Required steps are reported only by the enhance that changes the settings, so act on the
  first run's "Left for you" list
  ([KI-098](../reference/known-issues.md#ki-098-scaffold-enhance-reports-required-follow-ups-only-once)).

Both `enhance` (a settings change) and `upgrade` (to a new version) rewrite the manifest
without its comments.

## Migrations

The [changelog](../reference/changelog.md) lists every breaking change with its migration steps.
For 0.2.0 there are three:

<div class="grid cards gac-cols-3" markdown>

-   **[A running deployment](../reference/changelog.md#upgrading-a-running-deployment)**

    Roll out with `Recreate` or at one replica, create the metrics Secret, check the shutdown
    settings, update clients and eval datasets.

-   **[A project created with 0.1.0](../reference/changelog.md#upgrading-a-project-created-with-010)**

    Migrate `product-policy.yaml`, upgrade against the `v0.1.0` baseline, then port agent code
    and values files by hand.

-   **[A pre-release 0.2.0 build](../reference/changelog.md#upgrading-a-project-made-by-a-pre-release-020-build)**

    Find the commit that created the project and name it with `--baseline-ref`.

</div>

### 0.2 to 0.3 (unreleased)

0.3 adds [agents calling agents](multi-agent.md): the actor-aware principal, token exchange,
relayed approvals, `peer` and `system`; and
[structured final answers](develop.md#structured-final-answers), an agent that answers in a
JSON shape the project declares. An existing project keeps its behaviour after
`scaffold upgrade` unless one of the edits below applies to it. The complete list of changes
is in the [changelog](../reference/changelog.md).

#### In this order

1. **Update the CLI and the skills:** `graph-agents-cli update`. The skills carry the new
   rules for coding agents, including how to declare other agents.
2. **Upgrade the runtime:** `graph-agents-cli scaffold upgrade --dry-run`, then `-y`. It
   three-way merges the template-owned runtime (`app/app_utils/`, `app/fast_api_app.py`, the
   chart templates) and adds `app/app_utils/a2a_client.py`,
   `app/app_utils/token_exchange.py` and `app/app_utils/structured.py`. `app/agent.py`,
   `app/tools/**` and `app/policies/**` are never touched, and none of them needs a change
   unless the agent is to answer in a JSON shape (below). `.env.example` and the
   `values-<env>.yaml` files are yours and are not rewritten: the new settings are in the
   [environment reference](../reference/environment.md).
3. **Then declare other agents,** with `peer add` or `system apply`. `peer add` refuses a
   project whose runtime is still 0.2 and `system check` reports one (SC01): a 0.2 runtime
   refuses every call once `api-policy.yaml` uses `protocol: a2a`.
4. **After every later upgrade or `api` edit, run `graph-agents-cli peer sync`** in a project
   with peers: `scaffold upgrade` never touches `app/tools/`, and `lint` fails while
   `tools/a2a_peers.py` and the policy differ.
5. **Deploy** as [Rolling it out](#rolling-it-out) describes.

#### What needs your edit

- **An `auth: forward` API that can never send a credential** (under `shared-bearer`, under
  `jwt` without `forward_audience`, or under the `langgraph-server` runtime) stops the app
  from starting outside `APP_ENV=dev`, and `lint` reports it (exit 3). Every call to such an
  API already failed with "the caller has no credential". Give it `forward_audience`, move it
  to `auth: exchange`, or remove it (`scaffold upgrade` never rewrites `api-policy.yaml`);
  under `APP_ENV=dev` the app logs why and starts.
- **`jwt` reads the RFC 8693 `act` claim.** A token carrying one is an agent's for the user,
  refused with 403 until `AUTH_ALLOWED_ACTORS` lists that agent. If your identity provider
  already puts `act` in tokens that people use directly, list the agents or set
  `AUTH_JWT_ACTOR_CLAIM=` (empty) to read every token as the user's own, as 0.2 did.
- **Agents other agents call** list their callers in `AUTH_ALLOWED_ACTORS` (empty refuses
  every agent), with `AUTH_JWT_AUDIENCE` naming the agent. `system apply` sets both.
- **Custom policies** (`app/policies/**` is never upgraded): a policy through which another
  agent forwards users' credentials must set `Principal.actor`, or this agent treats the
  calling agent as the person. A policy that returns an empty id, one over 256 characters or
  one with control characters now fails the request with 500 and logs the bug.
- **Structured final answers, only if you adopt them.** A project answers in text until
  `app/response_schema.json` exists, and nothing in 0.2 creates that file. Before adding it,
  wire `app/agent.py` by hand, since `scaffold upgrade` never rewrites it: import
  `StructuredAnswer` and `response_format` from `app_utils.structured`, pass
  `response_format=response_format(model, tools)` to `create_agent`, and put
  `StructuredAnswer()` last in `middleware()`, as a new project's `agent.py` does. Then run
  `graph-agents-cli lint`: it refuses a schema the agent would not start with (exit 3) and
  warns while either piece of wiring is missing. Without the wiring, every run with a schema
  ends with the `error` code `invalid_structured_response`: the runtime checks every answer
  again before it delivers it on `/chat` and over A2A. An answer that did not go through
  `StructuredAnswer()` still stays in the thread, where the thread's messages and LangGraph
  Server's native API return it (KI-172), so wire both pieces. `scaffold upgrade` also brings
  the tests' `tests/conftest.py`, which runs them with the mode off
  (`RESPONSE_SCHEMA_PATH=none`), and `tests/unit/test_structured.py`, which checks your schema
  and that `agent.py` answers in it. See
  [A project created before 0.3](develop.md#structured-final-answers).
- **Hand-written peer clients** (a tool that posts A2A JSON-RPC itself, a delegating auth
  policy that exchanges tokens in `authenticate`): delete the delegating policy and its
  registration in `app/policies/__init__.py`, remove the APIs it used (`graph-agents-cli api
  remove <peer>_agent`, and any separate approvals API), declare each peer with
  `graph-agents-cli peer add <peer>` (or `system apply`), then delete the hand-written tool
  module.

#### What changes by itself

- **Database.** At startup, under the schema lock, `ADD COLUMN IF NOT EXISTS` adds
  `threads.actor`, `runs.actor` and the approvals columns `requester_actor`, `decide_with`,
  `relayers`, `decided_via` and `display_digest` (`agent_*` tables under
  `langgraph-server`); no row is rewritten, and existing rows read as direct. A pending
  approval asked before the upgrade is `direct` and is decided exactly as before. Under
  `langgraph dev` the approvals file is read as version 1 and written as version 2.
- **A2A tasks move to Postgres** under `CHECKPOINTER=postgres` (and a Postgres
  `DATABASE_URI` under `langgraph-server`): table `a2a_tasks` (`agent_a2a_tasks`), created at
  the next start. Every replica sees every task and tasks survive restarts; a task whose run
  ended with its process turns `failed`; a subscription or cancel that reaches a replica other
  than the one running the task is refused (`-32004`, `-32002`,
  [KI-024](../reference/known-issues.md#ki-024-a-running-a2a-tasks-subscription-and-cancel-work-only-on-the-replica-running-it)).
  `A2A_TASK_TTL_S=0` now keeps a task until its thread is deleted (0.2: until the process
  restarted). `CHECKPOINTER=memory` keeps the in-memory store.
- **A2A tasks follow their approval**: an `input-required` task ends as its approval does,
  also when the person decides over HTTP or the approval expires. The approval request part
  adds `approval_json` (the approvals as exact JSON text) and its text shows each call's body;
  a failed task carries an error data part (`{"type": "error", "code": ...}`); the agent card
  declares the origin extension. See [HTTP API](../reference/http-api.md#a2a).
- **The approval object gains fields** (`decide_with`, `requester_actor`, `decided_via`,
  `digest`, and for JSON-RPC calls `rpc_method` and `a2a_operation`), and its times read from
  Postgres are in UTC whatever the database session's time zone.
- **Correlation across agents.** Calls to other agents (`protocol: a2a`) and to `auth:
  forward` and `auth: exchange` APIs carry the request's `X-Request-ID` and, under OTLP
  tracing, its W3C trace context; an incoming `traceparent` on the A2A routes continues the
  caller's trace. Other APIs receive neither, and a `traceparent` on `/chat` is not continued.
  `PROPAGATE_TRACE_HEADERS=off` turns both off; `all` sends them to every API and continues a
  trace on every path. See [Observability](observability.md#across-agents-and-services).
- **Under `langgraph-server`, a native run acts for its caller**, whatever run context the
  request sends: its tools see the caller's own id, roles and actor. A client that set
  another principal's context on a native run no longer can (Studio under `langgraph dev`
  keeps the context it sends).
- **Tool output that is not valid Unicode** (a lone surrogate) reaches the model, the stream
  and A2A replies with U+FFFD in its place instead of failing the run.
- **`eval`'s `json_schema` check reads the reply's answer.** 0.2 parsed the first fenced
  block, else everything from the first `{` or `[`; 0.3 reads the whole reply when it is
  JSON, else its last JSON object or array (of the schema's root type), and in a project with
  a response schema the run's `structured_response` itself. A case that passed on an example
  the reply quoted before its answer can now fail, and one that failed on prose after the JSON
  can now pass. See [Evaluation](evaluation.md#deterministic-checks).
- **Streamed requests to OpenAI-API models ask for token usage** (`stream_options`), so runs
  on an `openai-compatible` endpoint record their tokens.
- **New settings default to 0.2's behaviour:** `MODEL_REASONING_EFFORT` and
  `MODEL_USE_RESPONSES_API` unset; no response schema (`RESPONSE_FORMAT_STRATEGY` acts only
  with one); no `limits.max_response_bytes`; `protocol: http` for every
  API; `decide_with: direct` for every gate; the `A2A_*` and `AUTH_*` delegation settings only
  act on requests other agents send.
- **The CLI** works under a SOCKS proxy (it depends on `httpx[socks]`), `run --stop-server`
  exits 2 when it cannot stop a server, `info` skips `npx skills list` under
  `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` or in CI, and `deploy` needs no Secret key for a
  keyless project.

#### Rolling it out

The new columns and the task table are created at startup under the schema lock, so new
pods can start beside old ones. Two things need care while both versions run:

- **Tasks.** Tasks a 0.2 pod holds in its memory are lost when it stops, as at any 0.2
  restart; tasks a 0.3 pod creates are in Postgres.
- **Wire callers last.** A 0.2 pod does not read `act`, so it takes another agent's token as
  the user's own. Add the callers to `AUTH_ALLOWED_ACTORS`, and declare peers in the calling
  agents, only once every pod of the called agent runs 0.3.

0.2.0 and 0.3 pods hold the same Postgres run lease, so they exclude each other on a thread.
A deployment still running 0.1.0 or a pre-release 0.2.0 build follows
[Upgrade a running deployment](#upgrade-a-running-deployment) instead.

#### Downgrading

`scaffold upgrade` has no downgrade guard
([KI-102](../reference/known-issues.md#ki-102-scaffold-upgrade-has-no-downgrade-guard)). Under
0.2 the added columns and the task table are ignored, A2A tasks go back to process memory,
and the threads agents started for a user are reachable by any token for that user, another
agent's included, since 0.2 does not read `act`.

### Upgrade a running deployment

Old pods (0.1.0 has no run lock across replicas; pre-release 0.2.0 builds used a session
advisory lock) and new pods (a Postgres lease) do not exclude each other. While both run, one
thread can run on an old and a new pod at once. Upgrade with a `Recreate` rollout or at one
replica.

!!! warning "The chart has no rollout strategy value"
    Even at one replica, the default `RollingUpdate` starts a new pod before the old one stops
    ([KI-032](../reference/known-issues.md#ki-032-the-chart-has-no-rollout-strategy-value-so-the-upgrade-advice-cannot-be-followed)).
    Scale the Deployment to 0 before the upgrade deploy, or patch its strategy to `Recreate` by
    hand.

The new tables are created at startup. With
`metrics.serviceMonitor.bearerToken.enabled`, run `graph-agents-cli secrets apply --env <env>`
once after the upgrade so `<release>-metrics` exists.

### A project created with 0.1.0

The authentic baseline is 0.1.0 itself, from the `v0.1.0` tag (commit `fc3f2f9`). When the tag
cannot be fetched (an offline mirror, a fork without tags), name the commit in any clone that
holds it:

```bash
git clone https://github.com/ss7172/graph-agents-cli /tmp/gac   # or your mirror
graph-agents-cli scaffold upgrade --baseline-ref /tmp/gac@fc3f2f9 --dry-run
graph-agents-cli scaffold upgrade --baseline-ref /tmp/gac@fc3f2f9 -y
```

Then port what `upgrade` never rewrites (`app/policies/__init__.py`, `app/agent.py`,
`app/tools/`, the `values-<env>.yaml` files) as the changelog describes.

!!! danger "Never `--baseline current` for a 0.1.0 project"
    Nearly every scaffolding file changed in 0.2.0, so the project would keep 0.1.0's
    `app_utils`, chart and workflows, its tests would fail to import and `/chat` would answer
    500. If you ran it, restore the backup it printed, or restore from git, and upgrade with
    the authentic baseline.

## Limitations

| Limitation | What to do |
|---|---|
| An edited chart `values.yaml` is kept whole, dropping new settings ([KI-040](../reference/known-issues.md#ki-040-scaffold-upgrade-keeps-an-edited-chart-valuesyaml-whole-dropping-new-settings)). | Compare with a fresh `create` and merge by hand. |
| A wrong `--baseline-ref` applied with `-y` records a stale project as up to date ([KI-041](../reference/known-issues.md#ki-041-a-wrong-baseline-ref-applied-with-y-records-a-stale-project-as-up-to-date)). | Always `--dry-run` first; re-run with the right baseline. |
| `scaffold upgrade` has no downgrade guard: an out-of-date CLI applies its older templates ([KI-102](../reference/known-issues.md#ki-102-scaffold-upgrade-has-no-downgrade-guard)). | Compare `graph-agents-cli --version` with the manifest's `cli_version` and `cli_build`, and preview with `--dry-run`. |
| The manifest loses its comments when `enhance` or `upgrade` rewrites it ([KI-096](../reference/known-issues.md#ki-096-the-manifests-comments-are-lost-when-a-command-rewrites-it)). | Restore them from version control. |
| Remote templates skip symlinks, and upstream scaffold-engine fixes are ported by hand ([KI-093](../reference/known-issues.md#ki-093-remote-templates-skip-symlinks-upstream-fixes-are-ported-by-hand)). | Use real files in templates. |

## Next steps

<div class="grid cards" markdown>

-   :material-file-cog-outline:{ .lg } **[Project manifest](../reference/manifest.md)**

    `cli_version`, `cli_build` and every other field.

-   :material-history:{ .lg } **[Changelog](../reference/changelog.md)**

    Breaking changes and migration steps for each release.

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    Roll the upgraded project out.

-   :material-numeric:{ .lg } **[Exit codes](../reference/exit-codes.md)**

    What 0, 1, 2 and 3 mean for every command.

</div>
