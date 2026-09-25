---
description: "Deploy a graph-agents-cli agent to any Kubernetes cluster with Helm: environments, the chart, local clusters, rollouts and rollback, and an external database."
---

# Deploy to Kubernetes

<p class="gac-lede">One command, <code>graph-agents-cli deploy --env &lt;env&gt;</code>, takes the
agent to any Kubernetes cluster with Helm. It checks everything it can before it changes
anything, rolls out with <code>--wait</code>, and undoes only its own failed revision.</p>

This page covers the three environments, what `deploy` does in each CD mode, local clusters,
the image, the rules a deploy follows, rollouts and rollback, the Helm chart and an external
database. How CI builds and promotes images is on [CI/CD](cicd.md); the app Secret is on
[Secrets](secrets.md).

## What you need

| Tool | Needed for |
|---|---|
| `helm`, `kubectl` | every deploy, `secrets`, `infra check` |
| `docker` (or a Docker-compatible CLI) with BuildKit | building the image (`build`, direct `deploy`); the Dockerfile's `RUN --mount` needs BuildKit (the `buildx` plugin, the default in Docker Desktop) |
| `git` | image tags (the short commit sha), `argocd` mode |
| `gh` (or `GITHUB_TOKEN`) | `argocd` mode: opening the pull request |

A tool missing from `PATH` makes `deploy` exit 2. The project needs the `kubernetes`
deployment target (the default of `create`; a `-p/--prototype` project has none) and a kube
context for the cluster.

## Environments

Every Kubernetes project has three environments. Each one has a values file, a namespace and
an app Secret, and under `argocd` an Argo CD `Application` in `deployment/argocd/`.

| | `dev` | `staging` | `prod` |
|---|---|---|---|
| Values file | `values-dev.yaml` | `values-staging.yaml` | `values-prod.yaml` |
| Namespace | `<name>-dev` | `<name>-staging` | `<name>-prod` |
| `APP_ENV` | `dev` | `staging` | `prod` |
| Database | bundled Postgres (and Redis for `langgraph-server`) | [external](#external-database), from the Secret | external, from the Secret |
| Traffic | none (Gateway off) | Gateway API `HTTPRoute` (or an `Ingress`) | `HTTPRoute` (or an `Ingress`) |
| App Secret `<name>-app` | optional (`secretOptional: true`) | required | required |
| Replicas | 1 | 1 | 2, with a PodDisruptionBudget and topology spread |
| Kube context | the current one is fine | recorded or confirmed | recorded or confirmed |

`<name>` is the project name, which is also the Helm release name. The manifest records each
environment's namespace and, once you set it, its kube context:

```yaml title="graph-agents-cli-manifest.yaml"
environments:
  dev: { context: "", namespace: my-agent-dev }
  staging: { context: "staging-cluster", namespace: my-agent-staging }
  prod: { context: "prod-cluster", namespace: my-agent-prod }
```

`deploy` passes `values.yaml` and then `values-<env>.yaml`, so an environment file lists only
what differs.

## What `deploy` does in each CD mode

The `--cd` choice at `create` time (or `scaffold enhance --cd` later) fixes how changes reach a
cluster. [CI/CD](cicd.md) covers what the generated workflows do in each mode.

| Mode | What `deploy --env <env>` does |
|---|---|
| `skip` (default) | Direct, for any environment: builds the image, side-loads it into a local cluster or pushes it, applies the app Secret from the env file, then `helm upgrade --install`. |
| `helm-push` | Runs Helm with an image (`--image`, or one it builds and pushes). `dev` is deployed directly; `staging` and `prod` are refused outside CI, even with `--image`, unless you pass `--force-direct`. It checks the live Secret but never applies it, and refuses `--env-file` and `--rotate-api-key`: the Secret owner runs [`secrets apply`](secrets.md). |
| `argocd` | Never runs Helm and contacts no cluster: opens a pull request that changes only `image.tag` in `values-<env>.yaml`, and Argo CD applies it after the merge. Refuses `--env-file` and `--rotate-api-key` like `helm-push`. `--status` and `--restart` do use the cluster. For a GitHub Enterprise Server remote, set `GH_HOST=<host>` (and `GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN`). |

"Outside CI" means the `GITHUB_ACTIONS` variable is not `true`. The `helm-push` refusal reads:

```text
Error: Refusing to deploy staging from outside CI in helm-push mode (with or without --image).
  The staging and promote-to-prod workflows run `deploy --env staging --image <ref>` on the self-hosted runner (GITHUB_ACTIONS=true); pass --force-direct to deploy from here anyway.
```

## Deploy to a local cluster

kind, k3d, minikube, k3s, Docker Desktop, Rancher Desktop and OrbStack all work. A side-loaded
image never leaves your machine, so any valid registry name will do.

```bash
kind create cluster --name agents      # or k3d, minikube, Docker Desktop
graph-agents-cli create my-agent --registry localhost/dev
cd my-agent
cp .env.example .env
graph-agents-cli login --write-env     # API_KEY and your provider key
graph-agents-cli deploy --env dev --dry-run
graph-agents-cli deploy --env dev
graph-agents-cli deploy --env dev --status
```

In `dev`, `deploy` reads `.env.dev`, else `.env`, and applies its allow-listed keys to the
Secret `my-agent-app`. The pods use the model provider in the chart values, so the Secret needs
that provider's key. To try a cluster without one, add `MODEL_PROVIDER: fake` under `env:` in
`values-dev.yaml`.

!!! tip "How the cluster is recognised"
    `deploy` identifies a local cluster from its nodes (a `kind://` provider id, k3d node
    names, the minikube label, Docker Desktop or OrbStack node names), then confirms it with
    the tool's own listing: `kind get clusters`, `k3d cluster list` or `minikube profile list`.
    The context name decides only when the nodes cannot be read, and a `kind-*` name is still
    confirmed with kind. Any other cluster gets the image pushed to the registry.

How the image reaches each kind of local cluster:

| Cluster | How the image is loaded |
|---|---|
| kind | `kind load docker-image <image> --name <cluster>` |
| k3d | `k3d image import <image> -c <cluster>` |
| minikube | `minikube image load <image>` (with `-p <profile>` for a named profile) |
| k3s | `docker save`, then `k3s ctr images import` (usually needs root) |
| Docker Desktop, Rancher Desktop, OrbStack | nothing to load: they share the Docker daemon |

The start of a dry run shows the plan, before the rendered manifests. Here the cluster could
not be reached, so the Secret could not be read and the mode falls back to pushing to the
registry; on a kind cluster the mode line reads `direct, local-load`:

```text
Environment: dev  namespace: my-agent-dev
Kube context: docs-unreachable (the kubeconfig's current context; server https://127.0.0.1:21249)
Mode: direct, registry (build, push, and helm upgrade) (its nodes cannot be read and the context name is not a dev cluster)
  ▸ kubectl get secret my-agent-app -o json -n my-agent-dev --context docs-unreachable
  [dry-run] could not read Secret my-agent-app (Command failed (exit code 1): kubectl get secret my-agent-app -o json -n my-agent-dev --context docs-unreachable); the real run refuses unless it holds: OPENAI_API_KEY, API_KEY
  [dry-run] docker build -t localhost/dev/my-agent:07e43d3 -f Dockerfile .
  [dry-run] docker push localhost/dev/my-agent:07e43d3
  No env file found (.env.dev or .env); the Secret my-agent-app is left as is.
  [dry-run] helm dependency build deployment/helm/my-agent
  [dry-run] fetching subchart(s) postgresql, redis into deployment/helm/my-agent/charts so the render below can run (local only).
  [dry-run] helm upgrade --install my-agent deployment/helm/my-agent -f deployment/helm/my-agent/values.yaml -f deployment/helm/my-agent/values-dev.yaml --set image.repository=localhost/dev/my-agent --set image.tag=07e43d3 --set existingSecret=my-agent-app --create-namespace --wait --timeout 5m -n my-agent-dev --kube-context docs-unreachable
  [dry-run] a failed rollout prints pod diagnostics and is rolled back (--no-atomic keeps it).
  [dry-run] rendering with `helm template` instead:
```

## The image

### The registry

The image is `<registry>/<name>:<tag>`. `create` takes the registry from `--registry`, else
from the git `origin` remote (`ghcr.io/<owner>`), else it records the placeholder
`ghcr.io/CHANGE-ME`, which `build` and `deploy` refuse with exit 3:

```text
Error: The image registry is still the placeholder 'ghcr.io/CHANGE-ME'.
  Run `graph-agents-cli scaffold enhance --registry <host>/<org>` (it sets create_params.registry in graph-agents-cli-manifest.yaml, image.repository in the chart's values.yaml and IMAGE_REPOSITORY in .github/agent.env), ...
```

Set it everywhere it is read with one command:

```bash
graph-agents-cli scaffold enhance --registry registry.example.com/team
```

### Building

[`build`](../reference/cli.md#graph-agents-cli-build) runs `docker build` with the runtime's
Dockerfile. Its default tag is `latest`; `--registry` overrides the manifest, `--push` pushes
and `--dry-run` prints the commands:

```console
$ graph-agents-cli build --tag 1.0.0 --push --dry-run
Dry run; would execute:
  docker build -t localhost/dev/my-agent:1.0.0 -f Dockerfile .
  docker push localhost/dev/my-agent:1.0.0
```

### Tags

`deploy` never deploys `latest`. The tag of a workstation build is:

- the short commit sha (`07e43d3`);
- `<sha>-dirty-<timestamp>` when the project has uncommitted changes, with a warning, so the
  rollout picks them up. Changes under `deployment/`, `.github/`, `tests/` and `docs/` do not
  count: they never reach the image;
- a timestamp outside git;
- whatever `--tag` says.

`--image` deploys an image that already exists instead of building one. It takes a tagged
reference; a digest (`repo@sha256:...`) is refused with exit 3, because the chart renders
`repository:tag` only. An invalid reference or a placeholder registry is refused before
`docker` runs.

!!! tip "Build once, deploy many"
    In direct mode every `deploy` builds again, so dev and staging can run two different
    builds under one tag ([KI-028](../reference/known-issues.md#ki-028-each-workstation-deploy-rebuilds-the-image-under-the-same-tag)).
    Push one build and deploy the later environments with `--image <ref>`, or use a CD mode,
    where CI builds each commit once.

## The rules a deploy follows

`deploy` checks everything that needs only the project first, then the cluster, and changes
nothing until every check has passed:

1. **The project:** the chart and the values file, the Gateway settings, the image reference,
   the env file, placeholders in the chart `env`, and the `jwt` settings.
2. **The context:** the kube context and its API server are printed, and confirmed outside `dev`.
3. **The Secret:** the Secret the deploy would produce (the live keys merged with the env file)
   must hold every required key. When one is missing, `deploy` exits 1 before anything is built
   or changed.
4. **The release:** a Helm operation already in progress on the release stops the deploy (exit 2).
5. **The changes:** the image is built and loaded or pushed, the namespace created when
   missing, the Secret applied and Helm run.

`--dry-run` makes the same checks (it reads the live Secret, read-only), so it refuses what
the real run would. It prints every command, runs `helm template` instead of `helm upgrade` and
never prompts. [`secrets apply`](secrets.md) follows the env-file and context rules below too.

### The env file

`deploy` reads `--env-file`, else `.env.<env>`. Only `dev` falls back to `.env`: any other
environment without an env file exits 3, so your development keys never reach staging or prod.

```text
Error: No env file for staging: pass --env-file or create .env.staging (the local .env is never used for staging: it holds your development keys) with the allow-listed keys: OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY
```

Only `skip` mode reads the env file and applies the Secret. `helm-push` checks the live Secret
without applying it; `argocd` never touches the cluster.

### The kube context

The context is `--context`, else `environments.<env>.context` in the manifest, else the
kubeconfig's current context. Outside `dev` the current context needs a confirmation: a prompt
at a terminal, `--yes` otherwise. Without either, `deploy` exits 1:

```text
Error: Refusing to deploy to staging on the kubeconfig's current context 'docs-unreachable' without confirmation.
  Record it as environments.staging.context in graph-agents-cli-manifest.yaml or pass --context <name>, or pass --yes to accept the current context.
```

A context that is named explicitly but missing from the kubeconfig is exit 3.

### Settings that cannot work

Some settings would give pods that cannot start or cannot answer. `deploy` refuses them outside
`dev` (exit 3) and warns in `dev`:

- **A `CHANGE-ME` placeholder in the chart `env`**, such as the base URL `api add` writes or the
  `OPENAI_BASE_URL` of an `openai-compatible` project. This applies in every CD mode.
- **Incomplete `jwt` settings.** Outside `dev` the chart `env` or the Secret must provide a
  verification key (`AUTH_JWT_JWKS_URL` or `AUTH_JWT_PUBLIC_KEY`), `AUTH_JWT_ISSUER` and
  `AUTH_JWT_AUDIENCE`. In `dev` a missing key source is a warning (the pods would answer 503).
- **A Gateway without a parent.** `values-staging.yaml` and `values-prod.yaml` enable the
  Gateway with a blank `gateway.parentRef.name`, which `deploy` refuses (exit 3) in `skip` and
  `helm-push` mode before anything runs. Set it (and `gateway.hostname`), or disable the
  Gateway and enable the Ingress.

A value listed in the chart `env`, even an empty one, overrides the same key in the Secret.

### Protected environments

`deploy --env staging` and `--env prod` are refused while the manifest says
`auth_policy_implemented: false`, which is what a `custom` auth policy starts with until you
implement it. See [Authentication](authentication.md).

### Chart dependencies

The chart declares the Bitnami `postgresql` and `redis` subcharts. `deploy` runs
`helm dependency build` when one is missing from `charts/`, even under `--dry-run`, which needs
`registry-1.docker.io` unless you vendor them under `deployment/helm/<name>/charts/`.

## Rollouts and rollback

### A rollout

`deploy` runs `helm upgrade --install --wait --timeout <--timeout>` (default `5m`). helm's
output is printed when it returns.

When the rollout fails, `deploy` prints the pods, their container states, this release's
warning events since the deploy started and the logs. Then, with `--atomic` (the default), it
rolls back to the newest good revision, or uninstalls a first install that never succeeded.
`--no-atomic` leaves the failed revision in place for inspection. Either way `deploy` exits 2.

It only ever undoes the revision it created. When another Helm operation holds the release (a
`pending-*` revision), `deploy` refuses up front (exit 2) and prints the command that clears a
lock left by an interrupted helm.

### The Secret after a failure

Once the release is back where it was (rolled back, failed before a new revision, or a first
install uninstalled), the app Secret this deploy applied is put back to its previous values,
and a Secret it created is deleted. A bad value never waits for the pods' next restart. A Secret
someone changed in the meantime is left alone.

When the release stays on the failed revision (`--no-atomic`, or another deploy's revision),
the Secret keeps the new values and the error names the changed keys.

### Redeploying the same image

Redeploying the image the release already runs is announced up front. Helm then records a new
revision but replaces no pod unless the chart values changed. When only the Secret changed,
`deploy` says so: running pods read the Secret only when they start, so run
`deploy --restart`.

### Status and restart

```bash
graph-agents-cli deploy --env staging --status      # waits up to 60s
graph-agents-cli deploy --env staging --restart     # after a Secret rotation
graph-agents-cli deploy --env staging --status --timeout 3m
```

| Command | What it does | Exit |
|---|---|---|
| `deploy --status` | Waits up to `--timeout` (default `60s`) for the rollout, then prints replicas, image, Helm revision and each pod's readiness and restarts. When the rollout is incomplete it adds the pods' states, warning events and logs. | 1 when not complete |
| `deploy --restart` | Restarts the Deployment and waits up to `--timeout` (default `5m`) for the new pods. The old pods keep serving until new ones are ready. | 2 with diagnostics when they do not become ready |

In `argocd` mode `--status` runs `argocd app get <name>-<env>` when the `argocd` CLI is
installed, and `--restart` warns that Argo CD's self-heal may revert the restart annotation.

!!! warning "Add the context to printed recovery commands"
    The `helm rollback`, `helm uninstall` and `kubectl rollout undo` commands `deploy` suggests
    after a failure name the release and namespace but not the kube context. Add
    `--kube-context <ctx>` (helm) or `--context <ctx>` (kubectl) before running one
    ([KI-029](../reference/known-issues.md#ki-029-recovery-commands-printed-by-deploy-leave-out-the-kube-context)).

## The chart

The chart lives in `deployment/helm/<name>/`. It renders a Deployment, a Service, a ConfigMap
from `env`, a ServiceAccount, and optionally an HTTPRoute or Ingress, a cert-manager
Certificate, an HPA, a PodDisruptionBudget, a NetworkPolicy and a ServiceMonitor.

### Pod security

The pods meet the restricted Pod Security Standard:

- uid and gid 1000, `runAsNonRoot`, no privilege escalation, every capability dropped;
- seccomp `RuntimeDefault`;
- a read-only root filesystem; a `/tmp` emptyDir (256Mi) is the only writable path, and `HOME`
  points there;
- no service-account token (`automountServiceAccountToken: false`): the agent never calls the
  Kubernetes API.

### Probes and resources

| Probe | Path | Meaning |
|---|---|---|
| Startup | `/health` | the process answers (up to 30 × 5 s for the first boot) |
| Liveness | `/health` | the process answers; failing restarts the pod |
| Readiness | `/ready` | the database answers within 2 s; failing takes the pod out of the Service without a restart |

Requests default to 100m CPU and 256Mi memory, with a 1Gi memory limit and no CPU limit. Prod
requests 250m and 512Mi. `/health`, `/ready` and `/metrics` are never published by the route.
[Observability](observability.md) covers the probes and metrics in detail.

### Values worth knowing

| Value | Default | Purpose |
|---|---|---|
| `image.tag` | `""` | The chart refuses to render without a tag and never defaults to `latest`. Quote it (`tag: "0123456"`): an unquoted number is refused. |
| `env` | model, auth, `A2A_*` settings | Non-secret settings, rendered into a ConfigMap. A key listed here overrides the Secret. |
| `existingSecret` | `""` (`<release>-app`) | The app Secret; `deploy` passes it. |
| `secretOptional` | `true` in dev, `false` elsewhere | Without the Secret, pods outside dev do not start. |
| `route.publicPaths` | `/chat` (Exact), `/threads`, `/a2a/<agent>` (PathPrefix) | What the HTTPRoute or Ingress publishes. The chart refuses an empty list. |
| `route.devPaths` | `/playground`, `/docs`, `/openapi.json` | Published only while `env.APP_ENV` is exactly `dev`. |
| `gateway.*` | enabled, blank parent | Set `gateway.parentRef.name` and `gateway.hostname` for staging and prod. |
| `ingress.*` | disabled | An `Ingress` instead of the Gateway (`className`, `hostname`, `annotations`). |
| `tls.*` | none | `tls.existingSecret`, or `tls.certManager.enabled` with an `issuerRef`. |
| `appUrl` | `""` | The public URL the A2A card advertises; derived from the hostname when empty. |
| `metrics.scrapeAnnotations`, `metrics.serviceMonitor.*` | off | Prometheus scraping; see [Observability](observability.md#scrape-it-from-the-chart). |
| `tracing.*` | off, `capture: metadata` | Sets `TRACING_ENABLED`, `TRACE_CAPTURE`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `LANGSMITH_PROJECT`. |
| `networkPolicy.*` | off | See [NetworkPolicy](#networkpolicy) below. |
| `shutdown.preStopSleepSeconds` | `5` | A stopping pod keeps serving while its endpoints drain. |
| `shutdown.drainSeconds` | `20` | Then it gets SIGTERM and finishes in-flight requests and streams. |
| `terminationGracePeriodSeconds` | `30` | Must be longer than the two above together; the chart refuses it otherwise. Raise all three for long runs. |
| `hpa.*`, `pdb.*`, `topologySpread.*` | off (PDB and spread on in prod) | Autoscaling (needs `resources.requests.cpu` and metrics-server), disruption budget, spreading over nodes. |
| `probes.*`, `resources` | see above | Probe timings; requests and limits. |
| `extraVolumes`, `extraVolumeMounts` | `[]` | Extra mounts, such as a database CA. |
| `postgresql.*`, `redis.*` | on in dev only | The Bitnami subcharts, pinned to exact chart versions and image digests. |

The bundled dev Postgres keeps its password in a chart-managed Secret that survives upgrades,
and a restart of its pod shuts Postgres down in fast mode (seconds, no crash recovery). Use an
external database, or another chart, in staging and prod.

### NetworkPolicy

`networkPolicy.enabled` is off in every environment
([KI-033](../reference/known-issues.md#ki-033-networkpolicy-is-off-by-default-in-every-environment)):
it needs a CNI that enforces NetworkPolicy and addresses only you know. Turned on, it admits
only the http port, from the `ingressFrom` sources when listed. With `restrictEgress`, egress
is limited to DNS and `egressTo`.

`deployment/helm/<name>/examples/networkpolicy.yaml` is a worked staging and prod example: in
from the Gateway's and Prometheus's namespaces; out to DNS, the database, the model endpoint and
HTTPS on public addresses only, with every private range and the metadata endpoint excluded.
Copy its `networkPolicy` block into `values-<env>.yaml` and replace every address marked
`CHANGE`. An abridged view:

```yaml title="values-prod.yaml (from examples/networkpolicy.yaml)"
networkPolicy:
  enabled: true
  ingressFrom:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: gateway-system   # CHANGE
  restrictEgress: true
  egressTo:
    - to:
        - ipBlock:
            cidr: 192.0.2.10/32                          # CHANGE: the database
      ports:
        - port: 5432
          protocol: TCP
```

`deploy --env <env> --dry-run` renders the policy. Once it is deployed, a pod in another
namespace must not reach the agent, and `/ready` must still answer 200.

## External database

In staging and prod, `POSTGRES_DSN` (or `DATABASE_URI` for `langgraph-server`) in the app
Secret points at a database you run. Give the agent a least-privileged role that owns its own
database. It creates its tables at startup and needs nothing else, and never a superuser:

```sql
CREATE ROLE agent LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE agent OWNER agent;
REVOKE ALL ON DATABASE agent FROM PUBLIC;
```

Require TLS and check the server's certificate:

```bash title=".env.prod"
POSTGRES_DSN=postgresql://agent:<password>@db.example.com:5432/agent?sslmode=verify-full&sslrootcert=/etc/db-ca/ca.crt
```

The DSN reaches psycopg unchanged, so every libpq parameter works. Mount the CA with
`extraVolumes` and `extraVolumeMounts`:

```yaml title="values-prod.yaml"
extraVolumes:
  - name: db-ca
    secret:
      secretName: db-ca          # a Secret (or ConfigMap) holding ca.crt
extraVolumeMounts:
  - name: db-ca
    mountPath: /etc/db-ca
    readOnly: true
```

For a publicly trusted certificate use `sslrootcert=system`, or set `PGSSLMODE` and
`PGSSLROOTCERT` in the chart `env`. `deploy`, `secrets apply` and `infra check` warn outside
`dev` when the DSN does not require TLS; the value is never printed. On the server, `hostssl`
entries in `pg_hba.conf` refuse clear-text connections.

How the app uses the database:

- One connection pool per process (`DB_POOL_MIN_SIZE` 1, `DB_POOL_MAX_SIZE` 10), health-checked
  on checkout. Size `max_connections` for replicas × (`DB_POOL_MAX_SIZE` + 1); the extra
  connection holds the run leases. Do not put a transaction-mode PgBouncer in front.
- Every connection gets `connect_timeout=5` and TCP keepalives unless the DSN sets them, so a
  dead database is noticed in seconds and the app is ready again seconds after Postgres is.
- A replica that starts while Postgres is unreachable stays up, answers `/ready` (and requests)
  with 503, and sets up its schema once Postgres answers, instead of crash-looping.
- Schema changes run under an advisory lock, so replicas can start together.
- Backups are yours: pending approvals live in the same database as the threads.

## Check the cluster

[`infra check --env <env>`](../reference/cli.md#graph-agents-cli-infra-check) is a read-only
report of what the environment needs; it creates nothing and exits 1 when a required item is
missing:

- the tools, the cluster and its Kubernetes version;
- the Gateway API when `gateway.enabled`, the ingress class, cert-manager, metrics-server when
  the HPA is on, and Argo CD in `argocd` mode;
- the namespace, image pull secrets, the app Secret and its required keys, the metrics token
  Secret, the `jwt` settings and whether an external DSN requires TLS;
- every unreplaced `CHANGE-ME` (registry, chart image, chart `env`, CODEOWNERS, Argo CD
  `repoURL`);
- where pending approvals are kept, and the GitHub settings of the CD modes (see
  [CI/CD](cicd.md#required-github-settings)).

A check the environment does not need is a `skip` row. Every command it prints runs as is.

```text
infra check: mode helm-push, env prod
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Check                      ┃ Status  ┃ Required ┃ Detail                     ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ tool: kubectl              │ ok      │ yes      │ found                      │
│ tool: helm                 │ ok      │ yes      │ found                      │
│ tool: docker               │ ok      │ yes      │ found                      │
│ tool: gh                   │ ok      │ yes      │ found                      │
│ cluster reachable          │ missing │ yes      │ The connection to the      │
│                            │         │          │ server 127.0.0.1:21249 was │
│                            │         │          │ refused - did you specify  │
│                            │         │          │ the right host or port?    │
│ placeholder: registry      │ ok      │ yes      │ registry.example.com/team  │
│ placeholder: chart env     │ ok      │ yes      │ none                       │
│ placeholder: CODEOWNERS    │ missing │ yes      │ 8 rule(s) name a CHANGE-ME │
│                            │         │          │ owner                      │
...
Missing required prerequisites: cluster reachable, placeholder: CODEOWNERS
```

## Limitations

| Limitation | What to do |
|---|---|
| **Concurrent deploys to one release.** `deploy` refuses while another Helm operation holds the release, but two narrow races remain: a failed revision of another deploy can be attributed to this run (and rolled back), and a deploy can apply its Secret just before helm refuses it ([KI-030](../reference/known-issues.md#ki-030-two-narrow-races-between-concurrent-deploys-to-one-release)). | Serialize deploys to one environment: one CI concurrency group, one operator at a time. |
| **A database that stops answering without closing its connections** (a paused host, a proxy that holds traffic) is found by `/ready` and TCP timeouts, not in seconds: requests on open connections can wait about a minute ([KI-017](../reference/known-issues.md#ki-017-a-database-that-stops-answering-without-closing-connections-stalls-requests)). | Set `keepalives_*` and `tcp_user_timeout` in the DSN; alert on `/ready`. |
| **The Bitnami subcharts come from `registry-1.docker.io`**, which rate-limits anonymous pulls; their images are pinned by digest, and a pin must be refreshed if the digest is withdrawn ([KI-082](../reference/known-issues.md#ki-082-the-bitnami-subcharts-come-from-docker-hub)). | Authenticate pulls, or vendor the charts under `charts/`. |
| **Rolling upgrades from an older build** can run one thread on an old and a new pod at once, and the chart has no rollout strategy value ([KI-032](../reference/known-issues.md#ki-032-the-chart-has-no-rollout-strategy-value-so-the-upgrade-advice-cannot-be-followed)). | See [Upgrading projects](upgrading.md#upgrade-a-running-deployment). |
| A failed reinstall after `helm uninstall --keep-history` rolls back to the uninstalled release ([KI-031](../reference/known-issues.md#ki-031-a-failed-reinstall-after-uninstall-keep-history-rolls-back-to-the-old-release)). | Avoid `--keep-history`. |
| A Secret-only change does not restart pods, and a failed first install leaves its namespace ([KI-075](../reference/known-issues.md#ki-075-secret-only-changes-and-failed-first-installs-need-a-manual-follow-up)). | `deploy --restart`; delete the namespace if unwanted. |
| helm's failure reason is printed on stdout ([KI-072](../reference/known-issues.md#ki-072-helms-failure-reason-is-printed-on-stdout)); the tag is passed with `--set` ([KI-074](../reference/known-issues.md#ki-074-deploy-passes-the-image-tag-with-set-not-set-string)); some chart values are validated late ([KI-081](../reference/known-issues.md#ki-081-gaps-in-the-charts-value-validation)). | Keep stdout in CI logs; keep `route` and `metrics` as maps. |
| The `langgraph-server` licence is not checked before a deploy ([KI-076](../reference/known-issues.md#ki-076-the-langgraph-server-licence-is-not-checked-before-a-deploy)). | Add the licence variable to `secrets.keys`. |
| Rollback was verified with helm 4.3, and k3d and minikube detection against fakes ([KI-077](../reference/known-issues.md#ki-077-some-deploy-paths-are-verified-in-a-narrow-set-of-environments)). | Run `deploy --dry-run` first there. |

## Next steps

<div class="grid cards" markdown>

-   :material-source-pull:{ .lg } **[CI/CD](cicd.md)**

    Build once per commit and promote through `helm-push` or Argo CD pull requests.

-   :material-key-chain-variant:{ .lg } **[Secrets](secrets.md)**

    Apply, check and rotate the app Secret.

-   :material-shield-lock-outline:{ .lg } **[Security & production](security.md)**

    The checklist before production traffic.

-   :material-console:{ .lg } **[`deploy` reference](../reference/cli.md#graph-agents-cli-deploy)**

    Every flag of `deploy`, `build`, `secrets` and `infra check`.

</div>
