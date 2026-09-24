# Secrets

Plain Kubernetes Secrets, provisioned per environment, restricted to allow-listed keys. No
secrets controller is required.

## Contract

- The chart references the app Secret by name (`existingSecret`, default `<release>-app`) and
  mounts it with `envFrom`. The chart never templates the app Secret.
- Type `Opaque`, one key per allow-listed variable, in namespace `<name>-<env>`.
- Outside dev the Secret is required (`secretOptional: false`): pods do not start without it
  (`CreateContainerConfigError`). `values-dev.yaml` sets `secretOptional: true`.
- Non-secret configuration is the chart's ConfigMap plus Deployment `env` (which overrides
  `envFrom`).

## Allow-listed keys (`secrets.keys` in the manifest)

Scaffold default:

| Key | When |
|---|---|
| `OPENAI_API_KEY` \| `ANTHROPIC_API_KEY` \| `GOOGLE_API_KEY` \| `MODEL_API_KEY` | the provider key for `model_provider` |
| `JUDGE_API_KEY` | always (defaults to the provider key at runtime) |
| `POSTGRES_DSN` | runtime `fastapi` |
| `DATABASE_URI`, `REDIS_URI` | runtime `langgraph-server` |
| `API_KEY` | `shared-bearer` only (the one policy that reads it; generated when missing). Projects created before this change keep it listed; it is harmless there |
| `LANGSMITH_API_KEY` | always (used only when tracing is enabled) |
| each `auth: bearer` API's `token_env` | when `api-policy.yaml` declares that API |
| `AUTH_JWT_SECRET` | added automatically (not listed) under `jwt` when the chart values or the env file opt into HS* (`AUTH_JWT_ALLOW_HS=true` or an HS* algorithm) |

Only these are exported from the env file, never the whole file. Add any other secret the
project uses to the list: for example `METRICS_TOKEN`, `PRINCIPAL_HASH_SALT`, a LangGraph
licence key, or a tool credential. `scaffold enhance` recomputes the list when the runtime or
provider changes (keys you added are kept).

`METRICS_TOKEN`, once the app Secret holds it, is also written alone into `<name>-metrics`, the
Secret the chart's ServiceMonitor reads its bearer token from
(`metrics.serviceMonitor.bearerToken`): the scraper then needs read access to that Secret only,
never to the app Secret with the provider key, the API tokens and the DSN.

## Required keys

`deploy` and `secrets status` treat these as required, following the environment's merged chart
values: the provider key (not for `openai-compatible` or `fake`), `API_KEY` under
`shared-bearer`, `AUTH_JWT_SECRET` under `jwt` when the chart values list an HS* algorithm, and
`POSTGRES_DSN` or `DATABASE_URI`/`REDIS_URI` unless the bundled subchart provides them or
`CHECKPOINTER=memory`. A key set as a plain chart `env` value is satisfied. Only keys in
`secrets.keys` can be required: removing one from the allow-list is how an environment opts out.

## Commands

```bash
graph-agents-cli secrets apply  --env <env> [--env-file <file>] [--context <ctx>] [--yes] [--rotate-api-key] [--dry-run]
graph-agents-cli secrets status --env <env> [--context <ctx>] [--strict] [--dry-run]
```

- **Env file:** `--env-file`, else `.env.<env>`. Only `dev` falls back to `.env` (a developer's
  local keys never reach staging or prod); any other environment without one exits 3 with the
  list of allow-listed keys.
- **Kube context:** `--context`, else `environments.<env>.context`, else the kubeconfig's
  current context, which outside dev needs a confirmation (`Apply the Secret for <env> on
  context '<ctx>'? [y/N]`) or `--yes`. The context and API server are printed first. An explicit
  context missing from the kubeconfig is exit 3.
- **`apply`** creates the namespace when it is missing, then applies `<name>-app` with
  `kubectl create secret generic <name>-app --from-env-file=<0600 temporary file> --dry-run=client -o yaml | kubectl apply --server-side --field-manager=graph-agents-cli --force-conflicts -f -`.
  The temporary file holds only the allow-listed keys and is deleted afterwards, so no value
  appears on a command line; server-side apply writes no `last-applied-configuration` annotation
  (one left by an older client-side apply is removed and reported). `--force-conflicts` makes
  the CLI the owner of the allow-listed keys: do not let another controller manage them.
- **Merge:** a key the env file sets replaces the live value; an allow-listed key the env file
  leaves out is kept from the live Secret (so a partial env file never deletes keys). Remove a key
  by dropping it from `secrets.keys` (the next apply removes it) or with `kubectl`.
- **`API_KEY`:** the live key wins. When the env file sets a different one, the live key is kept
  with a warning unless `--rotate-api-key` is passed (clients with the old key then get 401;
  restart the pods with `deploy --restart`). Under `shared-bearer` (the policy in the chart's
  `env.AUTH_POLICY`, else the manifest's), when it is in neither the env file nor the live
  Secret, a key (32 random bytes, hex) is generated, applied, and written to the env file (mode
  0600) after the apply succeeds; it is never printed. If writing the file fails, the kubectl
  command to read it back is printed. `jwt` and `custom` never get a generated key (one the env
  file or the live Secret already holds is kept; drop `API_KEY` from `secrets.keys` to remove
  it).
- Values must be single-line (the env-file format cannot carry a newline; exit 3 naming the
  key). Exit 2 when kubectl fails (including a `get secret` failure that is not `NotFound`, which
  is never treated as "generate a new key").
- **`apply --dry-run`** prints the pipeline and a redacted manifest; it reads nothing from the
  cluster, prints no key, and notes that allow-listed keys absent from the env file would be kept
  from the live Secret.
- Outside dev, `apply` warns when the external database's DSN does not require TLS (see
  `kubernetes.md`, "External database"); the value is never printed.
- **`status`** runs `kubectl get secret <name>-app -o json` and lists `present`, `missing
  required`, `missing optional` and `not in the allow-list` keys, never values. Exit codes
  (usable as a gate): 0 every required key present, 1 the Secret or a required key is missing
  (any allow-listed key with `--strict`), 2 kubectl failed (connection, credentials, RBAC), 3
  configuration error (unknown environment or context, no manifest). `--dry-run` prints the
  command only.
- `apply` is **not** refused under CI: keeping application secrets out of CI is the procedure
  below (the scaffolded workflows never hold them), not a runtime guard.
- In `helm-push` and `argocd` modes `deploy` never applies Secrets (`--env-file` and
  `--rotate-api-key` are refused there); helm-push `deploy` still checks the live Secret's
  required keys before helm runs.

## Direct-mode `deploy`

`deploy --env <env>` in `cd: skip` applies the Secret with the same rules as `secrets apply`,
but first checks, read-only, that the Secret it would produce holds every required key: when
one is missing it exits 1 before anything is built, pushed or changed, naming the keys and the
env file to add them to. `dev` without an env file leaves the Secret as it is (and still checks
it). `deploy --dry-run` makes the same read of the live Secret and refuses (exit 1) what the real
run would; when the cluster cannot be read it says the check could not be completed.

When the rollout fails and the release is put back where it was (rolled back, failed before a
new revision, or a first install uninstalled), `deploy` also puts the app Secret (and
`<name>-metrics`) back to the values it held before, byte for byte, with a `resourceVersion`
precondition; a Secret this deploy created is deleted. A Secret someone changed after this
deploy applied it is left alone. When the release stays on the failed revision (`--no-atomic`,
another deploy, an unreadable history), the Secret keeps the new values and the error names the
changed keys and how to re-apply the previous ones. When the pods are not replaced (same image
and chart values) but the Secret changed, `deploy` says to run `deploy --restart`.

## Ownership in CD modes

The manifest records `secrets.owner` (free text: a team or role). That owner creates the Secret
once per environment with `graph-agents-cli secrets apply --env <env>` (from `.env.<env>`) from a
workstation with cluster access, or with `kubectl create secret generic`. CI never holds
application secrets. Argo never manages the app Secret. External Secrets Operator or Sealed
Secrets can replace this procedure; drop the keys they manage from `secrets.keys`.

## Rotation

1. Put the new value in `.env.<env>`.
2. `graph-agents-cli secrets apply --env <env>` (add `--rotate-api-key` for `API_KEY`).
3. `graph-agents-cli deploy --restart --env <env>` (`kubectl rollout restart`), because an
   externally managed Secret does not change the pod template checksum. It waits until the new
   pods are ready (`--timeout`, default 5m) and exits 2 with the pods' states, events and logs
   when they are not (a bad value: the old pods keep serving). In argocd environments, prefer an
   Argo resource action; self-heal may revert the restart annotation.
4. For `API_KEY`, update the client application's configuration in the same window.

## What is never a secret

`MODEL_PROVIDER`, `MODEL_NAME`, `OPENAI_BASE_URL`, `CHECKPOINTER`, `AUTH_POLICY`,
`AUTH_READ_ACROSS_ROLES`, `AUTH_ADMIN_ROLES`, the `AUTH_JWT_*` settings except
`AUTH_JWT_SECRET`, each API's `base_url_env`, the limits and logging settings, `TRACING_ENABLED`,
`TRACE_CAPTURE`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`APP_ENV`, `PORT`: these live in `values-<env>.yaml` `env:`. Settings that appear only in the
env file never reach the pods (the CLI warns about HS* JWT settings found there).

## Checks

- `secrets status --env <env>` after `apply`: exit 0 means every required key is there.
- A pod in `CreateContainerConfigError` means the Secret is missing (outside dev).
- `infra check --env <env>` reports the app Secret's missing required keys (with a `secrets
  apply` command that runs as printed), the `<name>-metrics` token Secret when the ServiceMonitor
  sends a token, whether the external DSN requires TLS, and whether the image pull secret named
  in `imagePullSecrets` exists; that pull secret is an operator prerequisite, not managed by
  `secrets apply`.
