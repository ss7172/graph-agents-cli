# Secrets

Plain Kubernetes Secrets, provisioned per mode, restricted to allow-listed keys. No secrets
controller is required in this release.

## Contract

- The chart references the app Secret by name (`existingSecret`, default `<release>-app`) and
  mounts it with `envFrom`. The chart never templates a Secret.
- Type `Opaque`, one key per allow-listed variable present in the env file, in namespace
  `<name>-<env>`.
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
| `API_KEY` | always (shared-bearer) |
| `LANGSMITH_API_KEY` | always (used only when tracing is enabled) |
| each `auth: bearer` API's `token_env` | when `api-policy.yaml` declares that API |

Only these are exported from the env file, never the whole file. Edit the list in the manifest
when the project adds a secret variable (for example a tool credential); `scaffold enhance`
updates it when the provider changes.

## Commands

```bash
graph-agents-cli secrets apply  --env <env> [--env-file <file>] [--dry-run]   # default .env.<env>, else .env
graph-agents-cli secrets status --env <env> [--dry-run]                        # key names present, no values
```

- `apply` creates or updates `<name>-app` with
  `kubectl create secret generic <name>-app --from-env-file=<0600 temporary file> --dry-run=client -o yaml | kubectl apply -f -`
  (the temporary file holds only the allow-listed keys and is deleted afterwards, so no value
  appears on a command line or in the echoed command). It generates `API_KEY` (32 random bytes,
  hex) only when it is absent from the env file **and** from the live Secret, and prints it once;
  store it where the client application keeps its credentials. An existing key is read back
  (`kubectl get secret -o json`) and re-included, so a repeated `apply` or direct-mode `deploy`
  never rotates it: one key per environment. Values must be single-line (the env-file format
  cannot carry a newline; exit 3 naming the key). Exit 3 when no env file is found or it holds
  none of the allow-listed keys; exit 2 when kubectl fails (including a `get secret` failure that
  is not `NotFound`, which is never treated as "generate a new key").
- `apply --dry-run` prints the pipeline and a redacted manifest (`API_KEY: <redacted>`); it reads
  nothing from the cluster and prints no key, only a note that `API_KEY` would be kept or generated.
- `apply` is **not** refused under CI: the CLI does not check `CI` or `GITHUB_ACTIONS`. Keeping
  application secrets out of CI is the procedure below (the scaffolded workflows never hold
  them), not a runtime guard.
- `status` runs `kubectl get secret <name>-app -o json` and lists present and missing allow-listed
  keys (and any key outside the allow-list, dimmed); it exits 1 when the Secret is absent
  (kubectl's `(NotFound)`) or any allow-listed key is missing, 0 when all are present, and 2 with
  kubectl's own message when the call fails for another reason (connection, credentials, RBAC),
  never a fabricated "missing" list. `--dry-run` prints the command only.
- In `helm-push` and `argocd` modes `deploy` never touches Secrets.

## Per-environment inputs

`deploy --env <env>` (direct modes) and `secrets apply --env <env>` read `--env-file`, defaulting
to `.env.<env>` when present, else `.env`. Keep `.env.staging` and `.env.prod` out of git; local
development uses `.env`.

## Ownership in CD modes

The manifest records `secrets.owner` (free text: a team or role). That owner creates the Secret
once per environment with `graph-agents-cli secrets apply --env <env> --env-file <file>` from a
workstation with cluster access, or with `kubectl create secret generic`. CI never holds
application secrets. Argo never manages the Secret. External Secrets Operator or Sealed Secrets
can replace this procedure later.

## Rotation

1. Put the new value in the env file.
2. `graph-agents-cli secrets apply --env <env>`.
3. `graph-agents-cli deploy --restart --env <env>` (`kubectl rollout restart`), because an
   externally managed Secret does not change the pod template checksum. In argocd environments,
   prefer an Argo resource action; self-heal may revert the restart annotation.
4. For `API_KEY`, update the client application's configuration in the same window.

## What is never a secret

`MODEL_PROVIDER`, `MODEL_NAME`, `OPENAI_BASE_URL`, `CHECKPOINTER`, `AUTH_POLICY`,
`AUTH_READ_ACROSS_ROLES`, each API's `base_url_env`, `TRACING_ENABLED`, `TRACE_CAPTURE`,
`LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `APP_ENV`, `PORT`: these
live in `values-<env>.yaml` `env:`.

## Checks

- `secrets status --env <env>` after `apply`: every allow-listed key you expect should be listed.
- A pod in `CreateContainerConfigError` means the Secret or a key is missing.
- `infra check --env <env>` reports whether the image pull secret named in `imagePullSecrets`
  exists; that secret is an operator prerequisite, not managed by `secrets apply`.
