---
description: Apply, check and rotate the allow-listed Kubernetes Secret of a graph-agents-cli agent without printing a value.
---

# Secrets

<p class="gac-lede">Each environment keeps its secrets in one Kubernetes Secret,
<code>&lt;name&gt;-app</code>. Only allow-listed keys reach it, and no value ever appears in a
values file, a log, a workflow or a command line.</p>

## The allow-list

Secrets never live in the chart or its values files. The manifest's `secrets.keys` is the only
set of variables that can reach the cluster:

```yaml title="graph-agents-cli-manifest.yaml"
secrets:
  # Only these keys are exported from an env file into the environment's Secret.
  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY]
  # Team or role responsible for provisioning and rotating the Secret (free text).
  owner: ""
```

| Key | When it is listed |
|---|---|
| The provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `MODEL_API_KEY` for `openai-compatible`) | always |
| `JUDGE_API_KEY` | always (optional: the judge defaults to the provider key) |
| `POSTGRES_DSN`, or `DATABASE_URI` and `REDIS_URI` under `langgraph-server` | always (dev gets them from the bundled subcharts) |
| `API_KEY` | new projects under `shared-bearer` only, the one policy that reads it |
| `LANGSMITH_API_KEY` | always (optional: tracing and `eval submit`) |
| The `token_env` of every `auth: bearer` API | added and removed by `graph-agents-cli api add` and `api remove` |

Add any other secret your project uses to the list, for example `METRICS_TOKEN` or
`PRINCIPAL_HASH_SALT`. A key the env file sets but the list leaves out is ignored. Under `jwt`
with an HS* algorithm, `AUTH_JWT_SECRET` joins the list without being written there.

## Apply the Secret

1. **Put the values in an env file.** `secrets apply` reads `--env-file`, else `.env.<env>`.
   Only `dev` falls back to `.env`; staging and prod without an env file exit 3, so your
   development keys never reach them. Keep `.env.staging` and `.env.prod` out of git.

    ```bash title=".env.prod"
    OPENAI_API_KEY=sk-...
    POSTGRES_DSN=postgresql://agent:<password>@db.example.com:5432/agent?sslmode=verify-full&sslrootcert=/etc/db-ca/ca.crt
    ```

2. **Preview it.** `--dry-run` prints the kubectl pipeline and a redacted manifest, without
   contacting the cluster:

    ```console
    $ graph-agents-cli secrets apply --env staging --dry-run
    Secret my-agent-app in my-agent-staging from .env.staging
    Kube context: docs-unreachable (the kubeconfig's current context; server https://127.0.0.1:21249)
      [dry-run] staging has no recorded context; the real run asks to confirm 'docs-unreachable' (or needs --yes).
      API_KEY is not in the env file: it would be kept from the live Secret if present, otherwise generated and saved to the env file (nothing is written under --dry-run).
      Allow-listed keys absent from the env file are kept from the live Secret if present: JUDGE_API_KEY, POSTGRES_DSN, LANGSMITH_API_KEY.
      [dry-run] kubectl create namespace my-agent-staging --context docs-unreachable  (only when the namespace does not exist)
      [dry-run] kubectl create secret generic my-agent-app --from-env-file=TMP_ENV_FILE_WITH_ALLOW_LISTED_KEYS --dry-run=client -o yaml -n my-agent-staging --context docs-unreachable | kubectl apply --server-side --field-manager=graph-agents-cli --force-conflicts -f - -n my-agent-staging --context docs-unreachable
    apiVersion: v1
    kind: Secret
    metadata:
      name: my-agent-app
      namespace: my-agent-staging
    type: Opaque
    stringData:
      OPENAI_API_KEY: <redacted>
      API_KEY: <redacted>
    ```

3. **Apply it.**

    ```bash
    graph-agents-cli secrets apply --env prod --context prod-cluster
    ```

    It creates the namespace when it is missing and applies the Opaque Secret `<name>-app` with
    server-side apply. The values go through a 0600 temporary file and a pipe, so none appears
    on a command line or in a `last-applied-configuration` annotation (an old one is removed).
    Outside `dev` the kube context follows the same rule as `deploy`: recorded in
    `environments.<env>.context`, passed with `--context`, or confirmed (`--yes` without a
    terminal).

4. **Check it.**

    ```bash
    graph-agents-cli secrets status --env prod --context prod-cluster
    ```

    It prints the keys under `present`, `missing required`, `missing optional` and, for keys
    the Secret holds outside `secrets.keys`, `not in the allow-list`. It never prints a value.

### What an apply keeps

- **Keys the env file leaves out** are kept from the live Secret, so a partial env file never
  deletes the rest. To remove a key, drop it from `secrets.keys`; the next apply removes it.
- **Values must be single-line:** `--from-env-file` splits on newlines, so a multi-line value
  is refused.
- **`API_KEY`: the live key wins.** It is replaced only when the env file sets a different one
  *and* you pass `--rotate-api-key`; clients with the old key then get 401.
- **A missing `API_KEY` is generated** under `shared-bearer` (32 random bytes, hex) when
  neither the env file nor the Secret has one. It is written to the env file (mode 0600) after
  the apply succeeds and never printed. `jwt` and `custom` projects never get one.
- **`METRICS_TOKEN`**, when the Secret holds it, is also written alone into a second Secret,
  `<name>-metrics`, for the Prometheus ServiceMonitor. Prometheus then needs no access to the
  app Secret ([Observability](observability.md#scrape-it-from-the-chart)).

!!! warning "One owner per key"
    `--force-conflicts` makes the CLI the owner of the allow-listed keys. Do not let another
    controller, such as External Secrets, manage the same keys: each would overwrite the other.

## Required keys and `secrets status`

A key is required when the pods cannot run without it. The list follows the environment's
merged chart values, the same inputs the pods get:

| Required key | When |
|---|---|
| The provider key | unless the chart's `MODEL_PROVIDER` is `openai-compatible` (often keyless) or `fake` |
| `API_KEY` | under `shared-bearer` |
| `AUTH_JWT_SECRET` | under `jwt` with an HS* algorithm in `AUTH_JWT_ALGORITHMS` |
| `POSTGRES_DSN`, or `DATABASE_URI` and `REDIS_URI` | unless the bundled subchart provides them, or `CHECKPOINTER` is `memory` |

Only keys in `secrets.keys` are ever required, and a key set as a plain chart `env` value does
not need the Secret. `deploy` checks the same list against the Secret it would produce and
refuses (exit 1) before building anything.

| `secrets status` exit | Meaning |
|---|---|
| 0 | every required key is present (optional ones may be missing) |
| 1 | the Secret is missing, or a required key is; with `--strict`, any allow-listed key |
| 2 | kubectl failed: unreachable cluster, credentials, RBAC |
| 3 | configuration error: unknown environment or kube context, no manifest |

That makes it a gate for a script or a pre-deploy step:

```bash
graph-agents-cli secrets status --env prod --context prod-cluster || exit 1
```

## Rotate a key

1. Change the value in `.env.<env>`.
2. Run `graph-agents-cli secrets apply --env <env>`, adding `--rotate-api-key` for `API_KEY`.
3. Run `graph-agents-cli deploy --env <env> --restart`. Running pods read the Secret only when
   they start; the restart waits for the new pods and prints why when they do not become ready.

A direct `deploy` (`skip` mode) applies the Secret from the env file as part of the deploy,
with the same rules, and takes `--rotate-api-key` too.

## Who applies the Secret

| CD mode | Who runs `secrets apply` |
|---|---|
| `skip` | whoever deploys: a direct `deploy` applies the Secret from the env file (or run `secrets apply` separately) |
| `helm-push`, `argocd` | an operator, the manifest's `secrets.owner`, from a workstation with cluster access, once per environment and for each rotation |

In `helm-push` and `argocd` mode CI never holds application secrets. `deploy` only checks the
live Secret (`helm-push`) or never touches the cluster (`argocd`), and refuses `--env-file` and
`--rotate-api-key`. See [CI/CD](cicd.md#secrets-in-the-cd-modes).

## Limitations

| Limitation | What to do |
|---|---|
| A hand edit of `api-policy.yaml` that switches an API to `auth: bearer` or renames its `token_env` does not update `secrets.keys`, and nothing reports the drift ([KI-038](../reference/known-issues.md#ki-038-a-hand-edit-of-a-bearer-apis-token-variable-is-not-reflected-in-secretskeys)). | Add the variable to `secrets.keys` by hand, or change the API with `graph-agents-cli api`. |
| A Secret-only change does not restart the pods ([KI-075](../reference/known-issues.md#ki-075-secret-only-changes-and-failed-first-installs-need-a-manual-follow-up)). | `deploy --env <env> --restart`. |
| `secrets status` on a missing Secret lists every allow-listed key as missing, without the required/optional split ([KI-084](../reference/known-issues.md#ki-084-secrets-status-on-a-missing-secret-does-not-split-required-and-optional-keys)). | `secrets apply --dry-run` shows what would be applied. |
| `secrets apply --dry-run` does not read the live Secret, and for a `jwt` or `custom` project with an empty env file it stops with an empty key list ([KI-085](../reference/known-issues.md#ki-085-secrets-apply-dry-run-can-fail-with-an-empty-key-list)). | Fill the env file, or run without `--dry-run` against a Secret that holds the keys. |

## Next steps

<div class="grid cards" markdown>

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    How `deploy` checks the Secret before it changes anything.

-   :material-source-pull:{ .lg } **[CI/CD](cicd.md)**

    Why CI never holds application secrets in the CD modes.

-   :material-file-cog-outline:{ .lg } **[Project manifest](../reference/manifest.md)**

    `secrets.keys`, `secrets.owner` and `environments`.

-   :material-shield-lock-outline:{ .lg } **[Security & production](security.md)**

    Where secrets fit in the security model and the production checklist.

</div>
